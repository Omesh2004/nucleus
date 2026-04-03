import sys, os
sys.path.append('/app')
from api.main import ch_client

sql = """
        SELECT 
            if(JSONHas(metadata, 'device_type') AND length(JSONExtractString(metadata, 'device_type')) > 0, JSONExtractString(metadata, 'device_type'), 'mobile') as device,
            count() as value
        FROM feature_intelligence.events_raw
        WHERE tenant_id IN ('nexabank', 'safexbank') AND timestamp >= today() - 7
        GROUP BY device
"""
try:
    device_res = ch_client.query(sql, {})
    merged = {}
    colors = {"desktop": "#1a73e8", "mobile": "#4285F4", "tablet": "#8AB4F8"}
    for row in device_res:
        dev = row['device'].lower()
        if dev not in colors:
            dev = "mobile"
        raw_val = int(row['value'])
        if raw_val > 0:
            merged[dev] = merged.get(dev, 0) + raw_val
    print("merged:", merged)
except Exception as e:
    print("ERROR:", e)
