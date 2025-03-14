# worker.py

import json
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


# RabbitMQ connection
connection = pika.BlockingConnection(pika.ConnectionParameters(host='localhost'))
channel = connection.channel()
channel.queue_declare(queue='elastic_queue', durable=True)

def callback(ch, method, properties, body):
    message = json.loads(body)
    action = message['action']
    data = message['data']

    if action == 'create_plan':
        index_plan(data)
    elif action == 'update_plan':
        update_plan(data)
    elif action == 'delete_plan':
        delete_plan(data)
    elif action == 'create_service':
        index_service(data)
    elif action == 'update_service':
        update_service(data)
    elif action == 'delete_service':
        delete_service(data)

    ch.basic_ack(delivery_tag=method.delivery_tag)

def index_plan(plan):
    plan['join_field'] = "plan"  # Parent type
    plan_id = plan['objectId']
    linked_services = plan.pop('linkedPlanServices', [])

    es.index(index='plans', id=plan_id, body=plan, refresh=True)

    # Index linked services
    for service in linked_services:
        service['parent_id'] = plan_id  # Pass the parent ID to the child
        index_service(service)


def index_service(service):
    parent_id = service['parent_id']  # Ensure this is passed correctly
    service['join_field'] = {
        "name": "service",  # Child type
        "parent": parent_id  # Parent ID
    }
    es.index(index='plans', id=service['objectId'], body=service, routing=parent_id, refresh=True)


def update_plan(plan):
    plan_id = plan['objectId']
    linked_services = plan.pop('linkedPlanServices', [])

    es.update(index='plans', id=plan_id, body={'doc': plan}, refresh=True)
    # Index linked services
    for service in linked_services:
        service['parent_id'] = plan_id  # Pass the parent ID to the child
        service['join_field'] = {
            "name": "service",  # Child type
            "parent": plan_id  # Parent ID
        }
        update_service(service)

def delete_plan(plan):
    plan_id = plan['objectId']
    es.delete(index='plans', id=plan_id, refresh=True)

    # Delete child services
    es.delete_by_query(
        index='plans',
        body={'query': {'parent_id': {'type': 'service', 'id': plan_id}}},
        refresh=True
    )

def update_service(service):
    parent_id = service['parent_id']
    # Perform a search to find the service by its objectId within the context of its parent
    search_body = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"_id": service['objectId']}},  # Assumes 'objectId' is the document ID
                    {"has_parent": {
                        "parent_type": "plan",  # Assuming 'plan' is the parent type
                        "query": {"term": {"_id": parent_id}}
                    }}
                ]
            }
        }
    }
    search_response = es.search(index='plans', body=search_body)
    if search_response['hits']['total']['value'] == 0:
        # If no service found, index it as new
        index_service(service)
    else:
        # Update the existing document
        update_body = {
            "doc": service,
            "doc_as_upsert": True  # This will update if exists or insert if not exists
        }
        es.update(index='plans', id=service['objectId'], body=update_body, routing=parent_id, refresh=True)


def delete_service(service):
    parent_id = service['parent_id']
    es.delete(index='plans', id=service['objectId'], routing=parent_id, refresh=True)

channel.basic_qos(prefetch_count=1)
channel.basic_consume(queue='elastic_queue', on_message_callback=callback)

print('Worker is waiting for messages...')
channel.start_consuming()
