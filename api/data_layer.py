import asyncio
import time
import sys
import os
from datetime import datetime
from typing import Dict, Any

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from storage.client import ch_client

# In-memory store for precomputed analytics metrics
PRECOMPUTED_LAYER: Dict[str, Dict[str, Any]] = {}

def fetch_tenant_summaries() -> None:
    """Fetches key insights (Funnel, adoption, performance) for all active tenants."""
    try:
        # Get active tenants in the last 7 days
        sql_tenants = "SELECT DISTINCT tenant_id FROM feature_intelligence.events_raw WHERE timestamp >= today() - 7"
        tenants_res = ch_client.query(sql_tenants)
        tenants = [row['tenant_id'] for row in tenants_res] if tenants_res else []
        
        for tenant in tenants:
            if not tenant:
                continue
                
            tenant_str = str(tenant)
            
            # 1. Low Adoption
            sql_adoption = """
                SELECT event_name, sum(total_events) as count
                FROM feature_intelligence.daily_feature_usage
                WHERE tenant_id = %(tenant_id)s AND date >= today() - 7
                GROUP BY event_name
                HAVING count > 0 AND count < 15
            """
            low_adoption = list(ch_client.query(sql_adoption, {"tenant_id": tenant_str}))
            
            # 2. Trending Data
            sql_trending = """
                SELECT event_name, 
                       sumIf(total_events, date = today()) as today_count,
                       sumIf(total_events, date = today() - 1) as yesterday_count
                FROM feature_intelligence.daily_feature_usage
                WHERE tenant_id = %(tenant_id)s AND date >= today() - 1
                GROUP BY event_name
                HAVING yesterday_count > 0 AND today_count > yesterday_count * 1.5
            """
            trending = list(ch_client.query(sql_trending, {"tenant_id": tenant_str}))
            
            # 3. Performance / Errors
            sql_performance = """
                SELECT avg(if(JSONHas(metadata, 'response_time_ms'), JSONExtractFloat(metadata, 'response_time_ms'), 15)) as avg_rt,
                       countIf(lower(event_name) LIKE '%%error%%' OR lower(event_name) LIKE '%%fail%%') as error_events,
                       count() as total
                FROM feature_intelligence.events_raw
                WHERE tenant_id = %(tenant_id)s AND timestamp >= today() - 7
            """
            performance = list(ch_client.query(sql_performance, {"tenant_id": tenant_str}))
            
            # Store everything inside the memory layer
            PRECOMPUTED_LAYER[tenant_str] = {
                "timestamp": time.time(),
                "last_updated": datetime.utcnow().isoformat(),
                "low_adoption": low_adoption,
                "trending": trending,
                "performance": performance
            }
            
        print(f"Data layer refreshed for {len(tenants)} active tenants.")
    except Exception as e:
        print(f"Error refreshing data layer: {e}")

async def start_data_layer_refresh(interval_minutes: int = 30) -> None:
    """Run data layer extraction loop in the background."""
    print("Starting precomputed data layer refresh task...")
    while True:
        try:
            await asyncio.to_thread(fetch_tenant_summaries)
        except Exception as e:
            print(f"Data layer task exception: {e}")
        
        await asyncio.sleep(interval_minutes * 60)
