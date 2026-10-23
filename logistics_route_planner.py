"""Local logistics route-planning prototype for the Munakata / Kyushu OSM PBF.

This is a transparent route-time simulator, not a trained logistic-regression
model. It uses a locally saved OSM road graph, illustrative vehicle capacities,
and an explicitly synthetic time-of-day slowdown profile. It has no live traffic
or authoritative truck-dimension restriction data.

Run with: streamlit run logistics_route_planner.py
"""

from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

import folium
import networkx as nx
import numpy as np
import streamlit as st
from streamlit_folium import st_folium


st.set_page_config(page_title="Local Logistics Route Planner", page_icon="🚚", layout="wide")

APP_DIR = Path(__file__).resolve().parent
DEFAULT_PBF = Path(
    os.environ.get(
        "ETC2_OSM_PBF",
        r"C:\Users\sharm\OneDrive\Desktop\ANUJ\JAPAN\VS-CODE\research_code\kyushu-261020.osm.pbf",
    )
)
# Clip the large Kyushu PBF to a manageable road-network window around Munakata.
# Order is west, south, east, north (longitude/latitude).
ROUTING_BBOX = (130.25, 33.55, 130.90, 34.10)
MAP_CENTER = [33.80, 130.55]

VEHICLES = {
    "Small delivery van (illustrative)": {"payload_kg": 500, "volume_m3": 3, "speed_factor": 1.00},
    "Medium truck (illustrative)": {"payload_kg": 2000, "volume_m3": 12, "speed_factor": 0.92},
    "Large truck (illustrative)": {"payload_kg": 5000, "volume_m3": 25, "speed_factor": 0.84},
}

ROAD_SPEED_KMH = {
    "motorway": 80, "motorway_link": 45, "trunk": 65, "trunk_link": 40,
    "primary": 50, "primary_link": 35, "secondary": 40, "secondary_link": 30,
    "tertiary": 35, "tertiary_link": 25, "unclassified": 30,
    "residential": 30, "living_street": 20, "service": 15, "road": 25,
}
URBAN_ROADS = {"tertiary", "tertiary_link", "unclassified", "residential", "living_street", "service", "road"}


def safe_float(value: object, fallback: float) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else fallback
    except (TypeError, ValueError):
        return fallback


def tag_text(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        return str(next(iter(value), ""))
    return str(value) if value is not None else ""


def speed_from_osm(value: object, highway: str) -> float:
    """Use a numeric OSM maxspeed where present; otherwise a stated demo default."""
    raw = tag_text(value)
    match = re.search(r"\d+(?:\.\d+)?", raw)
    if not match:
        return float(ROAD_SPEED_KMH.get(highway, 30))
    speed = float(match.group())
    if "mph" in raw.lower():
        speed *= 1.60934
    return float(np.clip(speed, 8, 110))


@st.cache_resource(show_spinner="Reading the local Kyushu road map and preparing the route network…")
def load_drive_graph(pbf_path: str, modified: float):
    """Read only a Munakata-area driving graph from the local PBF; cache it in memory."""
    from pyrosm import OSM

    osm = OSM(pbf_path, bounding_box=list(ROUTING_BBOX))
    nodes, edges = osm.get_network(nodes=True, network_type="driving")
    graph = osm.to_graph(nodes, edges, graph_type="networkx", retain_all=False)
    del nodes, edges, osm

    for u, v, key, data in graph.edges(keys=True, data=True):
        highway = tag_text(data.get("highway", "residential")).split(",")[0].strip()
        if highway.startswith("["):
            highway = highway.strip("[] '")
        data["road_class"] = highway
        data["length_m"] = max(1.0, safe_float(data.get("length"), 1.0))
        data["base_speed_kmh"] = speed_from_osm(data.get("maxspeed"), highway)
        data["road_name"] = tag_text(data.get("name", ""))
    node_ids = list(graph.nodes)
    node_lons = np.array([safe_float(graph.nodes[n].get("x"), np.nan) for n in node_ids])
    node_lats = np.array([safe_float(graph.nodes[n].get("y"), np.nan) for n in node_ids])
    keep = np.isfinite(node_lons) & np.isfinite(node_lats)
    return graph, [n for n, ok in zip(node_ids, keep) if ok], node_lons[keep], node_lats[keep]


def nearest_node(lon: float, lat: float, ids: list, lons: np.ndarray, lats: np.ndarray):
    # At Munakata's latitude, this scaled coordinate distance is adequate for
    # choosing the nearest road node for a map click.
    score = (lats - lat) ** 2 + ((lons - lon) * math.cos(math.radians(lat))) ** 2
    return ids[int(np.argmin(score))]


def traffic_factor(hour: float, road_class: str, peak_multiplier: float, use_demo_profile: bool) -> float:
    """Illustrative travel-time multiplier, not measured traffic."""
    if not use_demo_profile:
        return 1.0
    normalized_hour = hour % 24
    is_peak = 7 <= normalized_hour < 9 or 16 <= normalized_hour < 19
    if is_peak:
        return peak_multiplier if road_class in URBAN_ROADS else 1 + (peak_multiplier - 1) * 0.55
    if normalized_hour >= 22 or normalized_hour < 6:
        return 0.94 if road_class in URBAN_ROADS else 0.97
    return 1.06 if road_class in URBAN_ROADS else 1.02


def chosen_edge(graph, u, v, weight_key: str):
    options = graph.get_edge_data(u, v) or {}
    if not options:
        return {}
    return min(options.values(), key=lambda e: safe_float(e.get(weight_key), float("inf")))


def route_edges(graph, path: list, vehicle: dict):
    result = []
    for u, v in zip(path[:-1], path[1:]):
        edge = chosen_edge(graph, u, v, "free_seconds")
        speed = max(5.0, safe_float(edge.get("base_speed_kmh"), 25) * vehicle["speed_factor"])
        length = max(1.0, safe_float(edge.get("length_m"), 1))
        result.append({
            "u": u, "v": v, "data": edge, "length_m": length,
            "speed_kmh": speed, "free_seconds": length / (speed * 1000 / 3600),
        })
    return result


def simulate_travel(edges: list, depart: datetime, peak_multiplier: float, use_demo_profile: bool):
    current = depart
    total_seconds = 0.0
    for edge in edges:
        road_class = edge["data"].get("road_class", "residential")
        factor = traffic_factor(current.hour + current.minute / 60, road_class, peak_multiplier, use_demo_profile)
        seconds = edge["free_seconds"] * factor
        current += timedelta(seconds=seconds)
        total_seconds += seconds
    return current, total_seconds


def edge_line_coordinates(graph, edges: list):
    line = []
    for edge in edges:
        data = edge["data"]
        geom = data.get("geometry")
        parts = list(getattr(geom, "geoms", [geom])) if geom is not None else []
        coords = []
        for part in parts:
            if hasattr(part, "coords"):
                coords.extend(list(part.coords))
        if not coords:
            u, v = edge["u"], edge["v"]
            coords = [
                (safe_float(graph.nodes[u].get("x"), 0), safe_float(graph.nodes[u].get("y"), 0)),
                (safe_float(graph.nodes[v].get("x"), 0), safe_float(graph.nodes[v].get("y"), 0)),
            ]
        u = edge["u"]
        start_x = safe_float(graph.nodes[u].get("x"), coords[0][0])
        start_y = safe_float(graph.nodes[u].get("y"), coords[0][1])
        d_first = (coords[0][0] - start_x) ** 2 + (coords[0][1] - start_y) ** 2
        d_last = (coords[-1][0] - start_x) ** 2 + (coords[-1][1] - start_y) ** 2
        if d_last < d_first:
            coords.reverse()
        for lon, lat in coords:
            point = [float(lat), float(lon)]
            if not line or point != line[-1]:
                line.append(point)
    return line


def base_picker_map(center=MAP_CENTER):
    m = folium.Map(location=center, zoom_start=12, tiles=None, control_scale=True)
    folium.TileLayer(
        tiles="https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png",
        attr='Map data: <a href="https://maps.gsi.go.jp/development/ichiran.html">GSI Japan</a>',
        name="GSI Standard Map",
        max_zoom=18,
    ).add_to(m)
    folium.Rectangle(
        bounds=[[ROUTING_BBOX[1], ROUTING_BBOX[0]], [ROUTING_BBOX[3], ROUTING_BBOX[2]]],
        color="#168aad", weight=1, fill=False, tooltip="Area covered by the local routing extract",
    ).add_to(m)
    return m


st.title("🚚 Local Logistics Route Planner")
st.caption("Choose pickup and delivery points, enter the load, and compare an estimated route and departure time.")
st.warning(
    "**Prototype, not live navigation.** The route follows the local OpenStreetMap road extract. "
    "There is no live traffic feed. Departure-time suggestions use a clearly labelled demo slowdown pattern. "
    "Vehicle capacities and speed differences below are example assumptions, and the route does not verify "
    "all truck height, weight, width, or local legal restrictions."
)

with st.sidebar:
    st.header("Local road file")
    pbf_text = st.text_input("OSM PBF file", value=str(DEFAULT_PBF))
    pbf_path = Path(pbf_text).expanduser()
    st.caption(f"Routing window: {ROUTING_BBOX[0]}–{ROUTING_BBOX[2]}°E, {ROUTING_BBOX[1]}–{ROUTING_BBOX[3]}°N (Munakata area)")
    st.divider()
    st.markdown("**Route calculation**")
    st.write("Road intersections are **nodes**; road segments connecting them are **edges**.")
    st.write("One-way road directions in the OSM extract are included in the driving graph.")

if not pbf_path.exists():
    st.error("The local `.osm.pbf` road file was not found. Check the path in the sidebar.")
    st.info("The project copy is expected at `C:\\Users\\sharm\\OneDrive\\Desktop\\ANUJ\\JAPAN\\VS-CODE\\research_code\\kyushu-261020.osm.pbf`.")
    st.stop()

with st.spinner("Preparing local road network. The first read can take a while; later reruns use a cached graph."):
    try:
        graph, node_ids, node_lons, node_lats = load_drive_graph(str(pbf_path), pbf_path.stat().st_mtime)
    except Exception as exc:
        st.error(f"Could not read the OSM PBF road network: {exc}")
        st.info("Use the separate `requirements-logistics-planner.txt` environment and confirm the PBF file is a readable Kyushu extract.")
        st.stop()

if graph.number_of_edges() == 0 or not node_ids:
    st.error("No usable driving roads were found in the selected local map area.")
    st.stop()

st.success(f"Local road graph ready: {graph.number_of_nodes():,} intersections/nodes and {graph.number_of_edges():,} directed road links in the Munakata window.")

if "route_origin" not in st.session_state:
    st.session_state.route_origin = {"lat": 33.8000, "lon": 130.5400}
if "route_destination" not in st.session_state:
    st.session_state.route_destination = {"lat": 33.7700, "lon": 130.4800}
if "picked_map_point" not in st.session_state:
    st.session_state.picked_map_point = None
st.session_state.setdefault("origin_lat", st.session_state.route_origin["lat"])
st.session_state.setdefault("origin_lon", st.session_state.route_origin["lon"])
st.session_state.setdefault("destination_lat", st.session_state.route_destination["lat"])
st.session_state.setdefault("destination_lon", st.session_state.route_destination["lon"])

st.subheader("1. Choose start and destination")
st.write("Click a location on the map, select whether it is the start or destination, then press the matching button. You can also type coordinates below.")
pick_map = base_picker_map()
click_result = st_folium(pick_map, height=420, use_container_width=True, returned_objects=["last_clicked"], key="logistics_map_picker")
if click_result and click_result.get("last_clicked"):
    st.session_state.picked_map_point = {
        "lat": float(click_result["last_clicked"]["lat"]),
        "lon": float(click_result["last_clicked"]["lng"]),
    }

pick_cols = st.columns([1, 1, 2])
with pick_cols[0]:
    if st.button("Set as start", use_container_width=True, disabled=st.session_state.picked_map_point is None):
        st.session_state.route_origin = dict(st.session_state.picked_map_point)
        st.session_state.origin_lat = st.session_state.picked_map_point["lat"]
        st.session_state.origin_lon = st.session_state.picked_map_point["lon"]
with pick_cols[1]:
    if st.button("Set as destination", use_container_width=True, disabled=st.session_state.picked_map_point is None):
        st.session_state.route_destination = dict(st.session_state.picked_map_point)
        st.session_state.destination_lat = st.session_state.picked_map_point["lat"]
        st.session_state.destination_lon = st.session_state.picked_map_point["lon"]
with pick_cols[2]:
    st.caption("Use the last map click by pressing the matching endpoint button.")
if st.session_state.picked_map_point:
    p = st.session_state.picked_map_point
    st.caption(f"Last map click: {p['lat']:.5f}, {p['lon']:.5f}. Click a button above to save it as an endpoint.")

coord_cols = st.columns(2)
with coord_cols[0]:
    st.markdown("**Start coordinates**")
    st.number_input("Start latitude", min_value=ROUTING_BBOX[1], max_value=ROUTING_BBOX[3], format="%.6f", key="origin_lat")
    st.number_input("Start longitude", min_value=ROUTING_BBOX[0], max_value=ROUTING_BBOX[2], format="%.6f", key="origin_lon")
with coord_cols[1]:
    st.markdown("**Destination coordinates**")
    st.number_input("Destination latitude", min_value=ROUTING_BBOX[1], max_value=ROUTING_BBOX[3], format="%.6f", key="destination_lat")
    st.number_input("Destination longitude", min_value=ROUTING_BBOX[0], max_value=ROUTING_BBOX[2], format="%.6f", key="destination_lon")

# Keep endpoint state synchronized with editable number fields.
origin = {"lat": st.session_state.origin_lat, "lon": st.session_state.origin_lon}
destination = {"lat": st.session_state.destination_lat, "lon": st.session_state.destination_lon}

st.subheader("2. Enter the delivery and vehicle need")
load_cols = st.columns(3)
with load_cols[0]:
    cargo_kg = st.number_input("Cargo weight (kg)", min_value=0.0, value=300.0, step=25.0)
with load_cols[1]:
    cargo_m3 = st.number_input("Cargo volume (m³)", min_value=0.0, value=1.5, step=0.25, help="1 m³ is a cube that is 1 metre on each side.")
with load_cols[2]:
    preference = st.selectbox("Route preference", ["Fastest estimated time", "Shortest distance"])

peak_multiplier = st.slider(
    "Demo peak-hour slowdown multiplier",
    min_value=1.0, max_value=2.0, value=1.30, step=0.05,
    help="1.30 means the assumed travel time on affected roads is 30% longer at demo rush hours. It is not measured traffic.",
)
use_demo_profile = st.checkbox("Use the demo weekday time-of-day pattern", value=True)
date_col, start_col, end_col = st.columns(3)
with date_col:
    planning_date = st.date_input("Delivery date", value=date.today())
with start_col:
    window_start = st.time_input("Earliest departure", value=time(6, 0))
with end_col:
    window_end = st.time_input("Latest departure", value=time(21, 0))

feasible_vehicles = [
    name for name, spec in VEHICLES.items()
    if cargo_kg <= spec["payload_kg"] and cargo_m3 <= spec["volume_m3"]
]
if not feasible_vehicles:
    st.error("The entered cargo exceeds all example vehicle capacities. Reduce/split the load or define a larger vehicle class with your project supervisor.")
    st.stop()
suggested_vehicle = feasible_vehicles[0]
selected_vehicle = st.selectbox("Suggested vehicle (smallest example class that fits)", feasible_vehicles, index=0)
vehicle = VEHICLES[selected_vehicle]
st.caption(f"Capacity assumption: up to {vehicle['payload_kg']:,} kg and {vehicle['volume_m3']} m³. These are editable prototype assumptions, not verified specifications for a real fleet.")

if window_end <= window_start:
    st.error("Latest departure must be later than earliest departure on the selected date.")
    st.stop()

if st.button("Plan route and departure time", type="primary", use_container_width=True):
    for point_name, point in [("Start", origin), ("Destination", destination)]:
        if not (ROUTING_BBOX[0] <= point["lon"] <= ROUTING_BBOX[2] and ROUTING_BBOX[1] <= point["lat"] <= ROUTING_BBOX[3]):
            st.error(f"{point_name} is outside the local routing area. Pick a point inside the blue outline.")
            st.stop()

    start_node = nearest_node(origin["lon"], origin["lat"], node_ids, node_lons, node_lats)
    end_node = nearest_node(destination["lon"], destination["lat"], node_ids, node_lons, node_lats)
    if start_node == end_node:
        st.error("Start and destination snapped to the same nearby road point. Move them farther apart.")
        st.stop()

    # Attach transparent weights to every one-way road edge.
    for u, v, key, data in graph.edges(keys=True, data=True):
        kmh = max(5.0, data["base_speed_kmh"] * vehicle["speed_factor"])
        data["free_seconds"] = data["length_m"] / (kmh * 1000 / 3600)
    route_weight = "length_m" if preference == "Shortest distance" else "free_seconds"
    try:
        path_nodes = nx.shortest_path(graph, start_node, end_node, weight=route_weight, method="dijkstra")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        st.error("No connected driving route was found between these points in the saved road extract.")
        st.stop()

    selected_edges = route_edges(graph, path_nodes, vehicle)
    distance_km = sum(e["length_m"] for e in selected_edges) / 1000
    first_depart = datetime.combine(planning_date, window_start)
    last_depart = datetime.combine(planning_date, window_end)
    candidates = []
    check_time = first_depart
    while check_time <= last_depart:
        arrival, seconds = simulate_travel(selected_edges, check_time, peak_multiplier, use_demo_profile)
        candidates.append((seconds, check_time, arrival))
        check_time += timedelta(minutes=15)
    if not candidates or candidates[-1][1] != last_depart:
        arrival, seconds = simulate_travel(selected_edges, last_depart, peak_multiplier, use_demo_profile)
        candidates.append((seconds, last_depart, arrival))
    estimated_seconds, best_departure, estimated_arrival = min(candidates, key=lambda item: item[0])
    average_speed = distance_km / (estimated_seconds / 3600) if estimated_seconds else 0

    route_coordinates = edge_line_coordinates(graph, selected_edges)
    if not route_coordinates:
        route_coordinates = [[graph.nodes[n]["y"], graph.nodes[n]["x"]] for n in path_nodes]
    route_map = folium.Map(location=route_coordinates[len(route_coordinates) // 2], zoom_start=13, tiles=None, control_scale=True)
    folium.TileLayer(
        tiles="https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png",
        attr='Map data: <a href="https://maps.gsi.go.jp/development/ichiran.html">GSI Japan</a>',
        name="GSI Standard Map", max_zoom=18,
    ).add_to(route_map)
    folium.PolyLine(route_coordinates, color="#0b73b7", weight=6, opacity=0.88, tooltip="Suggested OSM driving route").add_to(route_map)
    folium.Marker([origin["lat"], origin["lon"]], tooltip="Start", icon=folium.Icon(color="green", icon="play")).add_to(route_map)
    folium.Marker([destination["lat"], destination["lon"]], tooltip="Destination", icon=folium.Icon(color="red", icon="flag")).add_to(route_map)
    route_map.fit_bounds(route_coordinates)

    st.subheader("3. Recommended plan (prototype estimate)")
    result_cols = st.columns(5)
    result_cols[0].metric("Suggested vehicle", selected_vehicle.split(" (")[0])
    result_cols[1].metric("Road distance", f"{distance_km:.1f} km")
    result_cols[2].metric("Leave around", best_departure.strftime("%H:%M"))
    result_cols[3].metric("Estimated drive time", str(timedelta(seconds=round(estimated_seconds / 60) * 60))[:-3])
    result_cols[4].metric("Estimated arrival", estimated_arrival.strftime("%H:%M"))
    st.caption(f"Average estimated speed: {average_speed:.1f} km/h. This is distance ÷ estimated time, not live speed or a guarantee.")
    st.markdown(f"**Why this vehicle?** It is the smallest example class that can carry {cargo_kg:,.0f} kg and {cargo_m3:.2f} m³ within the stated demo capacities.")
    st.markdown(f"**Why this departure time?** Among 15-minute departure choices from {window_start.strftime('%H:%M')} to {window_end.strftime('%H:%M')}, this gave the shortest estimated drive time under the selected demo slowdown assumptions.")
    st_folium(route_map, height=610, use_container_width=True, returned_objects=[], key=f"route_result_{best_departure.isoformat()}")

    names = []
    for edge in selected_edges:
        name = edge["data"].get("road_name", "").strip()
        if name and name not in names:
            names.append(name)
    if names:
        st.write("Road names found in the map data: " + " → ".join(names[:12]))
    st.caption("The highlighted route follows the saved OSM road graph and one-way directions. It is a planning suggestion, not turn-by-turn navigation.")

with st.expander("What ‘logistics model’ means here"):
    st.write(
        "This prototype is a **route optimizer and travel-time simulator**: it represents intersections as points, "
        "roads as links, and compares paths using distance or estimated travel time. It is not logistic regression, "
        "which is a statistical method for predicting a yes/no outcome from labelled examples. To learn real arrival "
        "times or late-delivery probabilities, we would need real historical trips with departure times, arrival times, "
        "vehicle types, and traffic conditions."
    )
    st.write(
        "The OSM map file contains road geometry and some road tags; the demo rush-hour factors and vehicle capacities "
        "are assumptions we can replace later. Use real fleet payload/volume specifications and authoritative truck "
        "restriction data before relying on a route for an actual delivery."
    )

st.divider()
st.caption("Local OSM PBF is read from disk. GSI background map tiles need internet. No live traffic source is connected.")
