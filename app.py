
"""
Streamlit application for routing and visualizing 3D hiking trails in Polish National Parks.
Connects to a Neo4j graph database to calculate shortest or fastest paths using 
Tobler's hiking function and visualizes the results using Folium and Plotly.
"""

import streamlit as st
import folium
import plotly.graph_objects as go
from streamlit_folium import st_folium
from neo4j import GraphDatabase

# ==========================================
# 1. CONFIGURATION, DATABASE SETUP & GRAPH PROJECTION (SAFE INITIALIZATION)
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

st.markdown("""
    <style>
        .block-container {
            padding-top: 1rem; /* Reduce this number to push it even higher! */
            padding-bottom: 0rem;
        }
    </style>
""", unsafe_allow_html=True)

@st.cache_resource
def get_driver():
    """
    Initializes and caches a Neo4j database driver connection.

    Returns:
        neo4j.Driver: An active Neo4j driver instance.
    """
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

driver = get_driver()

@st.cache_resource
def init_db():
    """
    Initializes the Graph Data Science (GDS) in-memory projections for all parks.
    Iterates through the PARK_CENTERS dictionary and projects each park's graph 
    into memory if it does not already exist, optimizing subsequent routing queries.
    """
    def setup_graph_projection(tx, park_name):
        graph_name = "graph_" + "".join(e for e in park_name if e.isalnum())
        
        result = tx.run("CALL gds.graph.exists($g) YIELD exists RETURN exists", g=graph_name).single()
        
        if not result["exists"]:
            tx.run("""
            MATCH (n:Waypoint {park_name: $park})-[r:TRAIL_SEGMENT]->(m:Waypoint {park_name: $park})
            RETURN gds.graph.project(
                $g,
                n,
                m,
                {},
                { 
                    duration_seconds: r.duration_seconds, 
                    distance_3d: r.distance_3d 
                }
            )
            """, g=graph_name, park=park_name).consume()
            print(f"Neo4j: '{graph_name}' projected into memory.")
        else:
            print(f"Neo4j: '{graph_name}' already exists in memory. Skipping.")

    with driver.session() as session:
        for park in PARK_CENTERS.keys():
            session.execute_write(setup_graph_projection, park)

init_db()

# ==========================================
# 2. STATE MANAGEMENT & SIDEBAR
# ==========================================
st.sidebar.title("Polish National Parks")
selected_park = st.sidebar.selectbox("Choose a Region:", list(PARK_CENTERS.keys()), index=9)

st.sidebar.markdown("---")
st.sidebar.title("Routing Strategy")

STRATEGIES = {
    "Fastest Route": "duration_seconds",
    "Shortest Distance": "distance_3d"
}
selected_strategy = st.sidebar.radio("Optimize for:", list(STRATEGIES.keys()))
weight_property = STRATEGIES[selected_strategy]

if "current_park" not in st.session_state:
    st.session_state.current_park = selected_park
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

if st.session_state.current_park != selected_park:
    st.session_state.current_park = selected_park
    reset_route()
    st.rerun() 

if st.session_state.routing_strategy != weight_property:
    st.session_state.routing_strategy = weight_property
    st.rerun()

# ==========================================
# 3. ROUTING & MATH FUNCTIONS
# ==========================================
def get_nearest_node(tx, lat, lon, park_name):
    """
    Finds the nearest Neo4j waypoint node to a given set of geographical coordinates.

    Args:
        tx (neo4j.Transaction): The active Neo4j transaction.
        lat (float): The latitude of the target location.
        lon (float): The longitude of the target location.
        park_name (str): The name of the specific national park to search within.

    Returns:
        int or None: The Neo4j internal ID of the nearest waypoint node, or None if no nodes are found.
    """

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
    """
    Calculates the optimal hiking route between two nodes using Dijkstra's algorithm.

    Queries the Neo4j GDS in-memory graph for the specified park to find the 
    shortest path based on either duration or 3D distance.

    Args:
        tx (neo4j.Transaction): The active Neo4j transaction.
        start_id (int): The Neo4j node ID of the starting waypoint.
        end_id (int): The Neo4j node ID of the destination waypoint.
        weight_prop (str): The property to optimize for ('duration_seconds' or 'distance_3d').
        park_name (str): The name of the national park to route within.

    Returns:
        tuple: A 4-tuple containing:
            - route_nodes (list of dict): Details of each node along the path.
            - edge_distances (list of float): The distance of each segment.
            - total_time (float): Total estimated time in seconds.
            - total_dist (float): Total 3D distance in meters.
            Returns (None, None, None, None) if start and end are identical or no route is found.
    """

    if start_id == end_id:
        return None, None, None, None
        
    graph_name = "graph_" + "".join(e for e in park_name if e.isalnum())
        
    query = f"""
    MATCH (source:Waypoint {{id: $start_id}}), (target:Waypoint {{id: $end_id}})
    // -> FIX: We now query the isolated graph for the specific park
    CALL gds.shortestPath.dijkstra.stream('{graph_name}', {{
        sourceNode: source, targetNode: target, relationshipWeightProperty: '{weight_prop}'
    }})
    YIELD nodeIds
    WITH [nodeId IN nodeIds | gds.util.asNode(nodeId)] AS route_nodes
    
    UNWIND range(0, size(route_nodes)-2) AS i
    WITH route_nodes, i, route_nodes[i] AS n1, route_nodes[i+1] AS n2
    
    OPTIONAL MATCH (n1)-[r:TRAIL_SEGMENT]->(n2)
    
    WITH route_nodes, i, coalesce(min(r.duration_seconds), 0) AS edge_time, coalesce(min(r.distance_3d), 0) AS edge_dist
    ORDER BY i ASC 
    
    WITH route_nodes, 
         collect(edge_dist) AS edge_distances, 
         sum(edge_time) AS total_time, 
         sum(edge_dist) AS total_dist
         
    RETURN route_nodes, edge_distances, total_time, total_dist
    """
    result = tx.run(query, start_id=start_id, end_id=end_id).single()
    if result:
        return result["route_nodes"], result["edge_distances"], result["total_time"], result["total_dist"]
    return None, None, None, None

def calculate_elevation_stats(route_nodes):
    """
    Calculates elevation statistics for a given sequence of route nodes.

    Args:
        route_nodes (list of dict): A list of dictionary objects representing 
                                    the nodes on the route, each containing an 'elevation' key.

    Returns:
        dict: A dictionary containing:
            - 'max_el' (float): The highest elevation point on the route.
            - 'asc' (float): The cumulative elevation gain (ascent).
            - 'desc' (float): The cumulative elevation loss (descent).
    """

    total_asc, total_desc = 0, 0
    elevations = [node['elevation'] for node in route_nodes]

    for i in range(1, len(route_nodes)):
        n1, n2 = route_nodes[i-1], route_nodes[i]
        elev_change = n2['elevation'] - n1['elevation']
        if elev_change > 0: total_asc += elev_change
        elif elev_change < 0: total_desc += abs(elev_change)

    return {
        "max_el": max(elevations),
        "asc": total_asc,
        "desc": total_desc
    }

# ==========================================
# 4. VISUALIZATION FUNCTIONS 
# ==========================================
def build_map(route_nodes=None):
    """
    Constructs an interactive Folium map centered on the selected park or current view.

    If route nodes are provided, it renders the trail path as a polyline, marks waypoints 
    with elevation tooltips, and drops start/end markers. 

    Args:
        route_nodes (list of dict, optional): A list of node dictionaries representing 
                                              the calculated path. Defaults to None.

    Returns:
        folium.Map: The generated Folium map object ready to be rendered in Streamlit.
    """

    center = st.session_state.map_center if st.session_state.map_center else PARK_CENTERS[selected_park]
    zoom = st.session_state.map_zoom if st.session_state.map_zoom else 12
    
    m = folium.Map(location=center, zoom_start=zoom)
    
    if st.session_state.start_coords and not route_nodes:
        folium.Marker(st.session_state.start_coords, popup="Start", icon=folium.Icon(color="green")).add_to(m)
        
    if route_nodes:
        path_coords = [(node['latitude'], node['longitude']) for node in route_nodes]
                
        folium.PolyLine(path_coords, weight=5, color='blue', opacity=0.7).add_to(m)
        
        for idx, node in enumerate(route_nodes):
            folium.CircleMarker(
                location=(node['latitude'], node['longitude']), radius=4, color='white', weight=1,
                fill=True, fill_color='red', fill_opacity=0.8,
                tooltip=f"<b>Waypoint {idx + 1}</b><br>Elev: {node['elevation']:.0f}m"
            ).add_to(m)
            
        folium.Marker(path_coords[0], popup="Start", icon=folium.Icon(color="green", icon="play")).add_to(m)
        folium.Marker(path_coords[-1], popup="End", icon=folium.Icon(color="red", icon="stop")).add_to(m)
        
    return m

def build_chart(route_nodes, edge_distances):
    """
    Generates an elevation profile chart for the calculated hiking route.

    Creates an interactive Plotly area chart visualizing elevation changes against 
    cumulative 3D distance along the trail.

    Args:
        route_nodes (list of dict): A list of node dictionaries containing 'elevation'.
        edge_distances (list of float): A list of distances for each trail segment.

    Returns:
        plotly.graph_objs._figure.Figure: A Plotly Figure object representing the elevation profile.
    """

    distances, elevations, labels = [0], [route_nodes[0]['elevation']], ["Start"]
    cumulative_dist = 0
    
    for i in range(1, len(route_nodes)):
        dist = edge_distances[i-1] if (i-1) < len(edge_distances) else 0 
        
        cumulative_dist += dist
        distances.append(cumulative_dist)
        elevations.append(route_nodes[i]['elevation'])
        labels.append(f"Waypoint {i+1}")

    padding = max(20, (max(elevations) - min(elevations)) * 0.1)
    
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
    st.button("Reset Route", on_click=reset_route, width="stretch")
    
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
                
                hours = int(total_time // 3600)
                minutes = int((total_time % 3600) // 60)
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
    
    map_data = st_folium(m, height=500, width="stretch", key=f"map_{st.session_state.current_park}")

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
    st.plotly_chart(build_chart(route_nodes, edge_distances), width="stretch")