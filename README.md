# Demo - 1: FastAPI Plan Management REST API
This is a demo project showcasing a RESTful API built using FastAPI to manage Plan objects, with Redis used as the backend key-value datastore. The API supports Create, Read, and Delete operations with validation and ETag support.

## Features
POST: Create a new Plan and store it in Redis.
GET: Retrieve an existing Plan using its objectId, with support for ETag validation to return 304 Not Modified if the resource has not changed.
DELETE: Remove a Plan by objectId and return a success message.

## Endpoints

#### 1. POST /api/v1/plans
* Description: Create a new Plan.
* Request Body: The request payload must adhere to the following 
* JSON Schema:
    ``` json

    {
        "objectId": "string",
        "planCostShares": {
            "deductible": "integer",
            "_org": "string",
            "copay": "integer",
            "objectId": "string",
            "objectType": "string"
        },
        "linkedPlanServices": [
            {
            "linkedService": {
                "_org": "string",
                "objectId": "string",
                "objectType": "string",
                "name": "string"
            },
            "planserviceCostShares": {
                "deductible": "integer",
                "_org": "string",
                "copay": "integer",
                "objectId": "string",
                "objectType": "string"
            }
            }
        ],
        "planType": "string",
        "creationDate": "string",
        "_org": "string",
        "objectType": "string"
    }
    ```
* Response:
    `201 Created` with the objectId and an ETag header if the plan is successfully created.
    `400 Bad Request` if validation fails or the objectId already exists.


#### 2. GET /api/v1/plans/{objectId}
* Description: Retrieve a plan by its objectId.
* Headers:
If-None-Match: ETag header (optional) to check if the resource has changed.
* Response:
    `200 OK` with the plan data and an ETag header.
    `304 Not Modified` if the plan has not changed since the last request (based on ETag).
    `404 Not Found` if no plan exists with the given objectId.

#### 3. DELETE /api/v1/plans/{objectId}
* Description: Delete a plan by its objectId.
* Response:
    `200 OK` with a message confirming successful deletion.
    `404 Not Found` if no plan exists with the given objectId.