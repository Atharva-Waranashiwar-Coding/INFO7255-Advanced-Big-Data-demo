import hashlib
import json
import urllib.parse
from pathlib import Path
from fastapi import FastAPI, HTTPException, Body, Request, Header, Depends, Response, status
from fastapi.responses import JSONResponse
from jsonschema import validate, ValidationError
from redis import Redis
import requests
import jwt
from fastapi.security import HTTPBearer
from jwt import PyJWKClient
import pika
from elasticsearch import Elasticsearch

es = Elasticsearch([{'host': 'localhost', 'port': 9200, 'scheme': 'http'}])

def initialize_index():
    index_name = "plans"
    if not es.indices.exists(index=index_name):
        mappings = {
            "mappings": {
                "properties": {
                    "join_field": {  # This field establishes the parent-child linkage.
                        "type": "join",
                        "relations": {
                            "plan": ["planservice", "service"]  # Assuming both are children of plan.
                        }
                    },
                    "planCostShares": {"type": "object"},
                    "linkedPlanServices": {"type": "nested"},  # Using nested if they are not separate child documents.
                    "objectId": {"type": "keyword"},
                    "planStatus": {"type": "text"},
                    "creationDate": {"type": "date", "format": "dd-MM-yyyy"}
                }
            }
        }
        es.indices.create(index=index_name, body=mappings)
        print(f"Index '{index_name}' created successfully.")
    else:
        print(f"Index '{index_name}' already exists.")

# RabbitMQ setup
# Global variable to hold the connection and channel
rabbitmq_connection = None
rabbitmq_channel = None
def rabbitmq_setup():
    global rabbitmq_connection, rabbitmq_channel
    if rabbitmq_connection is None or rabbitmq_connection.is_closed:
        rabbitmq_connection = pika.BlockingConnection(
                pika.ConnectionParameters(
                    host='localhost',
                    port=5672,
                    heartbeat=600  # Adjust the heartbeat interval as necessary
                )
            )
        rabbitmq_channel = rabbitmq_connection.channel()
        rabbitmq_channel.queue_declare(queue='elastic_queue', durable=True)
        print("RabbitMQ connection established and queue declared.",rabbitmq_channel)
    return rabbitmq_channel


def publish_to_rabbitmq(action, data):
    message = {
        'action': action,
        'data': data
    }
    channel = rabbitmq_setup()
    channel.basic_publish(
        exchange='',
        routing_key='elastic_queue',
        body=json.dumps(message),
        properties=pika.BasicProperties(delivery_mode=2)
    )
    print("Message published to RabbitMQ.")

# Set up the bearer token security dependency
security = HTTPBearer()
redis_client = Redis(host="localhost", port=6379, db=0)

# Google public keys URL
GOOGLE_PUBLIC_KEYS_URL = "https://www.googleapis.com/oauth2/v3/certs"
CLIENT_ID = "290206950095-e1ubcspttu19de0vaguie6v20gtuk29d.apps.googleusercontent.com"
CLIENT_SECRET = "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"
REDIRECT_URI = "http://localhost:8000/auth/callback"

# Caching Google keys to avoid frequent calls
public_keys = {}

def fetch_google_public_keys():
    global public_keys
    response = requests.get(GOOGLE_PUBLIC_KEYS_URL)
    if response.status_code == 200:
        public_keys = response.json()
    else:
        raise HTTPException(status_code=500, detail="Failed to fetch Google public keys")

def verify_google_token(token: str = Depends(security)):
    print("Token received:", token.credentials)
    unverified_header = jwt.get_unverified_header(token.credentials)
    key_id = unverified_header.get("kid")

    if not public_keys:
        fetch_google_public_keys()

    key_data = next((key for key in public_keys["keys"] if key["kid"] == key_id), None)
    if not key_data:
        raise HTTPException(status_code=403, detail="Invalid token")

    jwk_client = PyJWKClient(GOOGLE_PUBLIC_KEYS_URL)
    signing_key = jwk_client.get_signing_key_from_jwt(token.credentials)

    try:
        decoded_token = jwt.decode(
            token.credentials,
            signing_key.key,
            algorithms=["RS256"],
            audience=CLIENT_ID
        )
        return decoded_token
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=403, detail="Invalid token")



def load_plan_schema():
    schema_path = Path(__file__).parent / "jsonSchemas/planSchema.json"
    with open(schema_path, 'r') as schema_file:
        return json.load(schema_file)

plan_schema = load_plan_schema()

def generate_etag(data: str) -> str:
    return hashlib.md5(data.encode('utf-8')).hexdigest()



app = FastAPI(dependencies=[Depends(verify_google_token)])
@app.on_event("startup")
async def startup_event():
    initialize_index()
    print("Elasticsearch index initialization complete.")
    rabbitmq_setup()
    print("RabbitMQ connection established.")

@app.get("/auth/callback")
async def google_callback(code: str):
    token_url = "https://oauth2.googleapis.com/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code"
    }
    response = requests.post(token_url, headers=headers, data=data)
    print(response.headers)
    if response.status_code == 200:
        tokens = response.json()
        access_token = tokens.get("access_token")
        id_token = tokens.get("id_token")
        return JSONResponse(content={"access_token": access_token, "id_token": id_token})
    else:
        raise HTTPException(status_code=response.status_code, detail="Failed to obtain tokens")

@app.post("/api/v1/plans", status_code=201)
async def create_plan(plan: dict = Body(...)):
    object_id = plan.get("objectId")
    try:
        validate(instance=plan, schema=plan_schema)
    except ValidationError as e:
        field_path = " -> ".join([str(p) for p in e.path]) if e.path else "unknown field"
        raise HTTPException(status_code=400, detail=f"Invalid data in {field_path}: {e.message}")

    if redis_client.exists(object_id):
        raise HTTPException(status_code=409, detail="Plan with this objectId already exists")

    plan_json = json.dumps(plan)
    redis_client.set(object_id, plan_json)
    etag = generate_etag(plan_json)
    publish_to_rabbitmq('create_plan', plan)

    return JSONResponse(content={"message": "Plan created", "objectId": object_id}, headers={"ETag": etag}, status_code=201)

@app.get("/api/v1/search/plans")
async def search_plans(query: str):
    search_body = {
        'query': {
            'multi_match': {
                'query': query,
                'fields': ['planStatus', '_org', 'objectType']
            }
        }
    }
    res = es.search(index='plans', body=search_body)
    return res

@app.get("/api/v1/plans/{object_id}/full")
async def get_full_plan(object_id: str):
    try:
        plan_res = es.get(index='plans', id=object_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Plan not found")

    plan = plan_res['_source']

    # Fetch linked services
    services_res = es.search(
        index='plans',
        body={
            'query': {
                'parent_id': {
                    'type': 'service',
                    'id': object_id
                }
            }
        }
    )
    services = [hit['_source'] for hit in services_res['hits']['hits']]
    plan['linkedPlanServices'] = services

    return plan

@app.get("/api/v1/plans/{object_id}" )
async def get_plan(object_id: str, if_none_match: str = Header(None)):
    plan_json = redis_client.get(object_id)
    if not plan_json:
        raise HTTPException(status_code=404, detail="Plan not found")

    server_etag = generate_etag(plan_json.decode('utf-8'))
    if if_none_match == server_etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    return JSONResponse(content=json.loads(plan_json), headers={"ETag": server_etag})

@app.put("/api/v1/plans/{object_id}")
async def update_plan(object_id: str, plan: dict = Body(...), if_match: str = Header(None)):
    existing_plan = redis_client.get(object_id)
    if not existing_plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    server_etag = generate_etag(existing_plan.decode('utf-8'))
    if if_match and if_match != server_etag:
        raise HTTPException(status_code=412, detail="ETag mismatch, resource has been modified")

    plan_json = json.dumps(plan)
    redis_client.set(object_id, plan_json)
    new_etag = generate_etag(plan_json)
    publish_to_rabbitmq('update_plan', plan)
    return JSONResponse(content={"message": "Plan updated"}, headers={"ETag": new_etag})

@app.patch("/api/v1/plans/{object_id}")
async def patch_plan(object_id: str, plan_patch: dict = Body(...), if_match: str = Header(None)):
    existing_plan_json = redis_client.get(object_id)
    if not existing_plan_json:
        raise HTTPException(status_code=404, detail="Plan not found")

    server_etag = generate_etag(existing_plan_json.decode('utf-8'))
    if if_match and if_match != server_etag:
        raise HTTPException(status_code=412, detail="ETag mismatch, resource has been modified")

    existing_plan = json.loads(existing_plan_json)
    
    # Handle merging of linkedPlanServices
    if "linkedPlanServices" in plan_patch:
        if "linkedPlanServices" in existing_plan:
            # Append new items to the existing linkedPlanServices array
            existing_plan["linkedPlanServices"].extend(plan_patch["linkedPlanServices"])
        else:
            # If linkedPlanServices does not exist in existing plan, add it directly
            existing_plan["linkedPlanServices"] = plan_patch["linkedPlanServices"]

    # Merge other top-level keys
    updated_plan = {**existing_plan, **{k: v for k, v in plan_patch.items() if k != "linkedPlanServices"}}

    try:
        validate(instance=updated_plan, schema=plan_schema)
    except ValidationError as e:
        field_path = " -> ".join([str(p) for p in e.path]) if e.path else "unknown field"
        raise HTTPException(status_code=400, detail=f"Invalid data in {field_path}: {e.message}")

    updated_plan_json = json.dumps(updated_plan)
    redis_client.set(object_id, updated_plan_json)
    new_etag = generate_etag(updated_plan_json)
    publish_to_rabbitmq('update_plan', updated_plan)
    return JSONResponse(content={"message": "Plan patched"}, headers={"ETag": new_etag})

@app.delete("/api/v1/plans/{object_id}", status_code=204)
async def delete_plan(object_id: str):
    plan_json = redis_client.get(object_id)
    if not plan_json:
        raise HTTPException(status_code=404, detail="Plan not found")

    # Load the plan to identify linked children
    plan = json.loads(plan_json)
    linked_services = plan.get("linkedPlanServices", [])

    # Delete children from Redis
    for service in linked_services:
        service_id = service.get("objectId")
        if service_id:
            redis_client.delete(service_id)
            publish_to_rabbitmq('delete_service', {'objectId': service_id, 'parent_id': object_id})

    # Delete the parent object
    redis_client.delete(object_id)
    publish_to_rabbitmq('delete_plan', {'objectId': object_id})
    # Return an empty response for 204 status
    return Response(status_code=204)
