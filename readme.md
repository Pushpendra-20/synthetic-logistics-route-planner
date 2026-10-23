# Local logistics route planner prototype

This is a separate Streamlit prototype. It uses the locally saved Kyushu OpenStreetMap PBF file to build a driving-road graph, lets you choose start and destination points on a map, chooses an example vehicle class that fits the entered cargo, and estimates route distance, departure time, driving time, and arrival time.

## Run on Windows

Double-click `start_logistics_planner.bat`. The first run creates a separate Python environment and installs the requirements. It needs internet for installation and for the GSI map background. The PBF road network itself is read locally from:

`C:\Users\sharm\OneDrive\Desktop\ANUJ\JAPAN\VS-CODE\research_code\kyushu-261020.osm.pbf`

The app opens in a browser, usually at `http://localhost:8501`. If another Streamlit app is already using that port, Streamlit will show a different local port.

## How to use

1. Wait for the local road graph to load. The first read may take a while; later reruns are cached.
2. Click the map, then choose **Set as start** or **Set as destination**. You can edit the latitude/longitude fields if needed.
3. Enter cargo weight and volume. The app suggests the smallest example vehicle class whose assumed capacity fits.
4. Choose shortest-distance or fastest-estimated route, a departure-time window, and whether to apply the demo time-of-day slowdown.
5. Press **Plan route and departure time**. The map shows the chosen road route and the summary shows estimated distance and times. The suggested departure is the 15-minute slot with the shortest estimated driving time inside the window you chose.

## Assumptions and limits

- **Not logistic regression:** this is route optimization plus a travel-time simulation. Real late-delivery prediction would need historical labelled deliveries.
- **No live traffic:** the optional weekday peak-hour pattern is an illustrative assumption, not a measurement from vehicles or traffic sensors.
- **Vehicle capacities:** 500 kg / 3 m³ (small van), 2,000 kg / 12 m³ (medium truck), and 5,000 kg / 25 m³ (large truck) are editable demo values, not specifications for a particular vehicle.
- **Vehicle restrictions:** the graph respects one-way directions recorded in OSM, but this prototype does not check all truck height, width, weight, access, or local delivery rules. Do not use it as authoritative navigation.
- **Road speeds:** numeric OSM `maxspeed` values are used where available; otherwise transparent road-class defaults are used. These are only for planning demonstrations.
- **Geography:** the app clips the PBF to a Munakata-area bounding box. Points must be inside the blue boundary.
- **Map tiles:** route geometry comes from the local PBF; the GSI basemap is online and may show Japanese labels.

## Research note

Do not combine these estimated travel times with the prior synthetic ETC2.0 event-rate results as if they were observed trips. This app is a separate logistics prototype; a later validated study would need approved real or carefully calibrated route and travel-time data.
