import osmnx as ox
import requests
import math
import time
from neo4j import GraphDatabase

# ==========================================
# 1. CONFIGURATION
# ==========================================
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "hikinggraph"

ox.settings.use_cache = True

POLISH_PARKS = [
    "Babiogórski Park Narodowy, Poland",
    "Białowieski Park Narodowy, Poland",
    "Biebrzański Park Narodowy, Poland",
    "Bieszczadzki Park Narodowy, Poland",
    "Park Narodowy Bory Tucholskie, Poland",
    "Drawieński Park Narodowy, Poland",
    "Gorczański Park Narodowy, Poland",
    "Park Narodowy Gór Stołowych, Poland",
    "Kampinoski Park Narodowy, Poland",
    "Karkonoski Park Narodowy, Poland",
    "Magurski Park Narodowy, Poland",
    "Narwiański Park Narodowy, Poland",
    "Ojcowski Park Narodowy, Poland",
    "Pieniński Park Narodowy, Poland",
    "Poleski Park Narodowy, Poland",
    "Roztoczański Park Narodowy, Poland",
    "Słowiński Park Narodowy, Poland",
    "Świętokrzyski Park Narodowy, Poland",
    "Tatrzański Park Narodowy, Poland",
    "Park Narodowy Ujście Warty, Poland",
    "Wielkopolski Park Narodowy, Poland",
    "Wigierski Park Narodowy, Poland",
    "Woliński Park Narodowy, Poland"
]
if __name__ == "__main__":
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    # ==========================================
    # 2. PREPARE DATABASE
    # ==========================================
    print("Wiping existing Neo4j database clean before nationwide ingest...")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
        # FIX: Create an index to drastically speed up edge creation
        session.run("CREATE INDEX waypoint_id IF NOT EXISTS FOR (w:Waypoint) ON (w.id)")

    # ==========================================
    # 3. MASTER NATIONWIDE PIPELINE LOOP
    # ==========================================
    for i, park_name in enumerate(POLISH_PARKS):
        print(f"\n=============================================")
        print(f"Processing {i+1}/{len(POLISH_PARKS)}: {park_name}")
        print(f"=============================================")
        
        try:
            print("Step 1: Downloading OSM walking trails...")
            hiking_filter = (
                '["highway"~"path|footway|steps|pedestrian|track|via_ferrata|service|unclassified|residential"]'
                '["area"!~"yes"]["access"!~"private"]'
            )
            
            graph = ox.graph_from_place(park_name, custom_filter=hiking_filter)
            print(f"   -> Found {len(graph.nodes)} intersections and {len(graph.edges)} trail segments.")

            print("Step 2: Fetching European DEM elevation data...")
            nodes_payload = []
            node_ids = list(graph.nodes)
            batch_size = 50  

            for i in range(0, len(node_ids), batch_size):
                batch = node_ids[i:i + batch_size]
                locations_str = "|".join([f"{graph.nodes[n]['y']},{graph.nodes[n]['x']}" for n in batch])
                url = f"https://api.opentopodata.org/v1/eudem25m?locations={locations_str}"
                
                max_retries = 3
                success = False
                
                for attempt in range(max_retries):
                    try:
                        response = requests.get(url, timeout=10)
                        response.raise_for_status() 
                        data = response.json()
                        
                        if 'results' in data:
                            for j, result in enumerate(data['results']):
                                node_id = batch[j]
                                elev = result['elevation']
                                elevation = float(elev) if elev is not None else None
                                
                                graph.nodes[node_id]['elevation'] = elevation
                                nodes_payload.append({
                                    "id": node_id, "lat": graph.nodes[node_id]['y'], 
                                    "lon": graph.nodes[node_id]['x'], "elev": elevation
                                })
                            success = True
                            break
                    except requests.exceptions.RequestException as e:
                        wait_time = 2 ** attempt
                        print(f"   [!] Batch failed ({e}). Retrying in {wait_time}s...")
                        time.sleep(wait_time)
                
                if not success:
                    print("   [!] CRITICAL: Batch failed completely. Marking elevation as missing.")
                    for n in batch:
                        graph.nodes[n]['elevation'] = None
                        nodes_payload.append({
                            "id": n, "lat": graph.nodes[n]['y'], 
                            "lon": graph.nodes[n]['x'], "elev": None
                        })
                time.sleep(.5)

            print("Step 3: Calculating 3D distances and hiking times...")
            edges_payload = []

            for u, v, key, data in graph.edges(keys=True, data=True):
                elev_start = graph.nodes[u].get('elevation')
                elev_end = graph.nodes[v].get('elevation')
                dist_2d = data.get('length', 0)
                
                if elev_start is None or elev_end is None:
                    elev_change = 0
                else:
                    elev_change = elev_end - elev_start
                    
                dist_3d = math.sqrt(dist_2d**2 + elev_change**2)
                slope = elev_change / dist_2d if dist_2d > 0 else 0
                
                speed_kmh = 6 * math.exp(-3.5 * abs(slope + 0.05))
                speed_ms = speed_kmh / 3.6
                duration_seconds = dist_3d / speed_ms if speed_ms > 0 else 0
                
                edges_payload.append({
                    "start_id": u, "end_id": v, "d2d": dist_2d, "d3d": dist_3d, 
                    "elev_chg": elev_change, "slope": slope, "duration": duration_seconds
                })

            print("Step 4: Pushing data into Neo4j...")
            with driver.session() as session:
                session.run("""
                    UNWIND $nodes AS n
                    MERGE (w:Waypoint {id: n.id})
                    SET w.latitude = n.lat, w.longitude = n.lon, 
                        w.elevation = n.elev, w.park_name = $park
                """, nodes=nodes_payload, park=park_name)
                
                # FIX: Changed to CREATE to support multigraphs, index makes matching instant
                session.run("""
                    UNWIND $edges AS e
                    MATCH (start:Waypoint {id: e.start_id})
                    MATCH (end:Waypoint {id: e.end_id})
                    CREATE (start)-[:TRAIL_SEGMENT {
                        distance_3d: e.d3d,
                        elevation_change: e.elev_chg,
                        slope: e.slope,
                        duration_seconds: e.duration,
                        park_name: $park
                    }]->(end)
                """, edges=edges_payload, park=park_name)
                
            print(f"Success! {park_name} is fully loaded.")

        except Exception as e:
            print(f"Failed to process {park_name}. Error: {e}")

    driver.close()
    print("\nALL DONE! The complete Polish National Park graph is now alive in Neo4j.")