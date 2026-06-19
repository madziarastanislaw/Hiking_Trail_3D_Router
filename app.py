import streamlit as st
import folium
import plotly.graph_objects as go
from streamlit_folium import st_folium
from neo4j import GraphDatabase

# ==========================================
# 1. CONFIGURATION, DATABASE SETUP & GRAPH PROJECTION
# ==========================================
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "hikinggraph"

PARK_CENTERS = {
    "Babiogórski Park Narodowy, Poland": [49.57, 19.53],
    "Białowieski Park Narodowy, Poland": [52.75, 23.85],
    "Biebrzański Park Narodowy, Poland": [53.53, 22.75],
    "Bieszczadzki Park Narodowy, Poland": [49.12, 22.58],
    "Park Narodowy Bory Tucholskie, Poland": [53.82, 17.55],
    "Drawieński Park Narodowy, Poland": [53.08, 15.93],
    "Gorczański Park Narodowy, Poland": [49.55, 20.15],
    "Park Narodowy Gór Stołowych, Poland": [50.47, 16.33],
    "Kampinoski Park Narodowy, Poland": [52.32, 20.62],
    "Karkonoski Park Narodowy, Poland": [50.80, 15.55],
    "Magurski Park Narodowy, Poland": [49.52, 21.47],
    "Narwiański Park Narodowy, Poland": [53.07, 22.88],
    "Ojcowski Park Narodowy, Poland": [50.20, 19.82],
    "Pieniński Park Narodowy, Poland": [49.42, 20.40],
    "Poleski Park Narodowy, Poland": [51.45, 23.18],
    "Roztoczański Park Narodowy, Poland": [50.60, 22.97],
    "Słowiński Park Narodowy, Poland": [54.72, 17.25],
    "Świętokrzyski Park Narodowy, Poland": [50.87, 20.92],
    "Tatrzański Park Narodowy, Poland": [49.25, 19.98],
    "Park Narodowy Ujście Warty, Poland": [52.58, 14.73],
    "Wielkopolski Park Narodowy, Poland": [52.27, 16.80],
    "Wigierski Park Narodowy, Poland": [54.02, 23.05],
    "Woliński Park Narodowy, Poland": [53.93, 14.45]
}

st.set_page_config(page_title="Hiking Trail 3D Router", layout="wide")

@st.cache_resource
def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

driver = get_driver()

def ensure_graph_loaded(park_name):
    """
    Ensures only the currently selected park is projected into GDS memory.
    Drops any other loaded graphs to prevent Java Heap OutOfMemory errors.
    """
    graph_name = "graph_" + "".join(e for e in park_name if e.isalnum())
    
    with driver.session() as session:
        # 1. Check if the graph we want is already loaded
        exists = session.run("CALL gds.graph.exists($g) YIELD exists RETURN exists", g=graph_name).single()["exists"]
        
        if exists:
            return # It's already in RAM, do nothing!
            
        # 2. If we need to load a new graph, first DROP all currently loaded graphs to free up RAM
        print("Clearing old graphs from memory...")
        session.run("""
            CALL gds.graph.list() YIELD graphName 
            CALL gds.graph.drop(graphName, false) YIELD graphName AS dropped
            RETURN dropped
        """)
        
        # 3. Project ONLY the newly selected park
        print(f"Projecting {park_name} into memory...")
        query = """
        MATCH (n:Waypoint {park_name: $park})-[r:TRAIL_SEGMENT]->(m:Waypoint {park_name: $park})
        RETURN gds.graph.project(
            $g,
            n,
            m,
            {
                relationshipProperties: {
                    duration_seconds: toFloat(coalesce(r.duration_seconds, 0.0)), 
                    distance_3d: toFloat(coalesce(r.distance_3d, 0.0))
                }
            }
        )
        """
        session.run(query, g=graph_name, park=park_name).consume()

# ==========================================
# 2. STATE MANAGEMENT & SIDEBAR
# ==========================================
st.sidebar.title("Polish National Parks")
selected_park = st.sidebar.selectbox("Choose a Region:", list(PARK_CENTERS.keys()), index=9)

STRATEGIES = {"Fastest Route": "duration_seconds", "Shortest Distance": "distance_3d"}
selected_strategy = st.sidebar.radio("Optimize for:", list(STRATEGIES.keys()))
weight_property = STRATEGIES[selected_strategy]

# Initialize session state variables
if "current_park" not in st.session_state:
    st.session_state.current_park = selected_park
    # -> NEW: Load the graph on the very first boot
    ensure_graph_loaded(selected_park) 

if "routing_strategy" not in st.session_state:
    st.session_state.routing_strategy = weight_property
if "start_coords" not in st.session_state:
    st.session_state.start_coords = None
if "end_coords" not in st.session_state:
    st.session_state.end_coords = None
if "last_processed_click" not in st.session_state:
    st.session_state.last_processed_click = None
if "map_center" not in st.session_state:
    st.session_state.map_center = None
if "map_zoom" not in st.session_state:
    st.session_state.map_zoom = None

def reset_route():
    """
    Resets the active routing state in the Streamlit session.
    Clears the start coordinates, end coordinates, last processed clicks, 
    and the map's zoom and center states.
    """
    st.session_state.start_coords = None
    st.session_state.end_coords = None
    st.session_state.last_processed_click = None
    st.session_state.map_center = None
    st.session_state.map_zoom = None

# Handle user changing the selected park
if st.session_state.current_park != selected_park:
    st.session_state.current_park = selected_park
    # -> NEW: Swap the graph in memory when the user changes the dropdown
    ensure_graph_loaded(selected_park) 
    reset_route()
    st.rerun() 

# Handle user changing the optimization strategy
if st.session_state.routing_strategy != weight_property:
    st.session_state.routing_strategy = weight_property
    st.rerun()

# ==========================================
# 3. ROUTING & MATH FUNCTIONS
# ==========================================
def get_nearest_node(tx, lat, lon, park_name):
    query = """
    MATCH (w:Waypoint {park_name: $park})
    WITH w, point({latitude: w.latitude, longitude: w.longitude}) AS db_point, 
            point({latitude: $lat, longitude: $lon}) AS target_point
    RETURN w.id AS nearest_id, point.distance(db_point, target_point) AS dist_meters
    ORDER BY dist_meters ASC LIMIT 1
    """
    result = tx.run(query, lat=lat, lon=lon, park=park_name).single()
    return result["nearest_id"] if result else None

def get_route(tx, start_id, end_id, weight_prop, park_name):
    if start_id == end_id:
        return None, None, None, None
        
    graph_name = "graph_" + "".join(e for e in park_name if e.isalnum())
        
    query = f"""
    MATCH (source:Waypoint {{id: $start_id}}), (target:Waypoint {{id: $end_id}})
    CALL gds.shortestPath.dijkstra.stream('{graph_name}', {{
        sourceNode: source, targetNode: target, relationshipWeightProperty: '{weight_prop}'
    }})
    YIELD nodeIds
    WITH [nodeId IN nodeIds | gds.util.asNode(nodeId)] AS route_nodes
    
    UNWIND range(0, size(route_nodes)-2) AS i
    WITH route_nodes, i, route_nodes[i] AS n1, route_nodes[i+1] AS n2
    
    // FIX: Directed optional match ensures Tobler's function logic applies to the correct orientation!
    OPTIONAL MATCH (n1)-[r:TRAIL_SEGMENT]->(n2)
    
    WITH route_nodes, i, coalesce(min(r.duration_seconds), 0) AS edge_time, coalesce(min(r.distance_3d), 0) AS edge_dist
    ORDER BY i ASC 
    
    WITH route_nodes, collect(edge_dist) AS edge_distances, sum(edge_time) AS total_time, sum(edge_dist) AS total_dist
    RETURN route_nodes, edge_distances, total_time, total_dist
    """
    result = tx.run(query, start_id=start_id, end_id=end_id).single()
    if result:
        return result["route_nodes"], result["edge_distances"], result["total_time"], result["total_dist"]
    return None, None, None, None

def calculate_elevation_stats(route_nodes):
    total_asc, total_desc = 0, 0
    
    # Filter out None values to calculate the maximum elevation safely
    valid_elevations = [node['elevation'] for node in route_nodes if node['elevation'] is not None]
    max_el = max(valid_elevations) if valid_elevations else 0

    for i in range(1, len(route_nodes)):
        n1_elev = route_nodes[i-1].get('elevation')
        n2_elev = route_nodes[i].get('elevation')
        
        # Only calculate change if BOTH nodes have valid elevation data
        if n1_elev is not None and n2_elev is not None:
            elev_change = n2_elev - n1_elev
            if elev_change > 0: 
                total_asc += elev_change
            elif elev_change < 0: 
                total_desc += abs(elev_change)

    return {"max_el": max_el, "asc": total_asc, "desc": total_desc}

# ==========================================
# 4. VISUALIZATION FUNCTIONS 
# ==========================================
def build_map(route_nodes=None):
    center = st.session_state.map_center if st.session_state.map_center else PARK_CENTERS[selected_park]
    zoom = st.session_state.map_zoom if st.session_state.map_zoom else 12
    m = folium.Map(location=center, zoom_start=zoom)
    
    if st.session_state.start_coords and not route_nodes:
        folium.Marker(st.session_state.start_coords, popup="Start", icon=folium.Icon(color="green", icon="play")).add_to(m)
        if st.session_state.end_coords:
            folium.Marker(st.session_state.end_coords, popup="End", icon=folium.Icon(color="red", icon="stop")).add_to(m)
        
    if route_nodes:
        path_coords = [(node['latitude'], node['longitude']) for node in route_nodes]
        folium.PolyLine(path_coords, weight=5, color='blue', opacity=0.7).add_to(m)
        
        for idx, node in enumerate(route_nodes):
            # Safely format the elevation text for the tooltip
            elev = node.get('elevation')
            elev_text = f"{elev:.0f}m" if elev is not None else "Unknown"
            
            folium.CircleMarker(
                location=(node['latitude'], node['longitude']), radius=4, color='white', weight=1,
                fill=True, fill_color='red', fill_opacity=0.8,
                tooltip=f"<b>Waypoint {idx + 1}</b><br>Elev: {elev_text}"
            ).add_to(m)
            
        folium.Marker(path_coords[0], popup="Start", icon=folium.Icon(color="green", icon="play")).add_to(m)
        folium.Marker(path_coords[-1], popup="End", icon=folium.Icon(color="red", icon="stop")).add_to(m)
        
    return m

def build_chart(route_nodes, edge_distances):
    # Safely handle the starting elevation
    start_elev = route_nodes[0].get('elevation')
    start_elev = start_elev if start_elev is not None else 0
    
    distances, elevations, labels = [0], [start_elev], ["Start"]
    cumulative_dist = 0
    
    for i in range(1, len(route_nodes)):
        dist = edge_distances[i-1] if (i-1) < len(edge_distances) else 0 
        cumulative_dist += dist
        distances.append(cumulative_dist)
        
        # If elevation is missing, carry over the last known elevation to bridge the gap in the graph
        node_elev = route_nodes[i].get('elevation')
        safe_elev = node_elev if node_elev is not None else elevations[-1]
        elevations.append(safe_elev)
        
        labels.append(f"Waypoint {i+1}")

    padding = max(20, (max(elevations) - min(elevations)) * 0.1) if elevations else 20
    
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=distances, y=elevations, mode='lines+markers', name='Elevation', text=labels,
        hovertemplate='<b>%{text}</b><br>Dist: %{x:.0f}m<br>Elev: %{y:.0f}m<extra></extra>',
        fill='tozeroy', line=dict(color='royalblue', width=3), marker=dict(size=8, color='darkred')
    ))
    fig.update_layout(
        title=f"Elevation Profile ({selected_park.split(',')[0]})", 
        xaxis_title="Cumulative 3D Distance (meters)", 
        yaxis_title="Elevation (meters)",
        yaxis=dict(range=[min(elevations) - padding, max(elevations) + padding]),
        hovermode="closest", margin=dict(l=40, r=40, t=60, b=40), height=350
    )
    return fig

# ==========================================
# 5. STREAMLIT USER INTERFACE
# ==========================================
st.title("Polish National Parks 3D Router")
st.markdown(f"**Current Region:** {selected_park}")
st.markdown("Click anywhere on the map to set your **Start Point**, then click again to set your **End Point**.")

col1, col2 = st.columns([3, 1])

with col2:
    st.button("Reset Route", on_click=reset_route, width='stretch')
    
    if not st.session_state.start_coords:
        st.info("Waiting for Start Point click...")
    elif not st.session_state.end_coords:
        st.success("Start Point set!")
        st.info("Waiting for End Point click...")

route_nodes = None
if st.session_state.start_coords and st.session_state.end_coords:
    with driver.session() as session:
        start_id = session.execute_read(get_nearest_node, st.session_state.start_coords[0], st.session_state.start_coords[1], selected_park)
        end_id = session.execute_read(get_nearest_node, st.session_state.end_coords[0], st.session_state.end_coords[1], selected_park)
        
        if start_id and end_id:
            route_nodes, edge_distances, total_time, total_dist = session.execute_read(get_route, start_id, end_id, weight_property, selected_park)
            if route_nodes:
                elev_stats = calculate_elevation_stats(route_nodes)
                hours, minutes = int(total_time // 3600), int((total_time % 3600) // 60)
                dist_km = total_dist / 1000
                
                with col2:
                    st.metric("Estimated Time", f"{hours}h {minutes}m")
                    st.metric("Total Distance", f"{dist_km:.2f} km")
                    st.metric("Total Ascent", f"+{elev_stats['asc']:.0f} m")
                    st.metric("Total Descent", f"-{elev_stats['desc']:.0f} m")
                    st.metric("Max Elevation", f"{elev_stats['max_el']:.0f} m")
            else:
                with col2:
                    st.error("No valid trail path found. Try resetting the route.")

with col1:
    m = build_map(route_nodes)
    map_data = st_folium(m, height=500, width=700, key=f"map_{st.session_state.current_park}")

    if map_data and map_data.get("last_clicked"):
        lat = map_data["last_clicked"]["lat"]
        lon = map_data["last_clicked"]["lng"]
        current_click = (lat, lon)
        
        if current_click != st.session_state.last_processed_click:
            st.session_state.last_processed_click = current_click
            
            if map_data.get("center"):
                st.session_state.map_center = [map_data["center"]["lat"], map_data["center"]["lng"]]
            if map_data.get("zoom"):
                st.session_state.map_zoom = map_data["zoom"]
            
            if not st.session_state.start_coords:
                st.session_state.start_coords = current_click
                st.rerun() 
            elif not st.session_state.end_coords:
                st.session_state.end_coords = current_click
                st.rerun()

if route_nodes:
    st.plotly_chart(build_chart(route_nodes, edge_distances), width='stretch')