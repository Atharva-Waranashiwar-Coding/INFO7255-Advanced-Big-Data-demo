from elasticsearch import Elasticsearch

es = Elasticsearch([{'host': 'localhost', 'port': 9200, 'scheme': 'http'}])

if not es.indices.exists(index='plans'):
    es.indices.create(
        index='plans',
        body={
            'mappings': {
                'properties': {
                    'join_field': {
                        'type': 'join',
                        'relations': {'plan': 'service'}
                    },
                    # Additional mappings
                }
            }
        }
    )
