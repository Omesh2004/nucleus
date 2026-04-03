import sys, os
sys.path.append('/app')
from api.main import ch_client

sql = """
        SELECT 
            if(JSONHas(metadata, 'device_type') AND length(JSONExtractString(metadata, 'device_type')) > 0, JSONExtractString(metadata, 'device_type'), 'mobile') as device,
            count() as value
        FROM feature_intelligence.events_raw
        WHERE tenant_id = 'nexabank' AND timestamp >= today() - 7
        GROUP BY device
"""
try:
    device_res = ch_client.query(sql, {})
    for row in device_res:
        print("ROW:", row)
except Exception as e:
    print("ERROR:", e)
