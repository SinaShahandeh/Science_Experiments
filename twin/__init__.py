"""Digital twin of the egg-white rig: a simulated mhapi-service for checking agent runs before going live.

It serves the same HTTP API as the real service (Model_Hardware_API_spec.md, "CLI and service") on
http://127.0.0.1:8743, so the `mhapi` CLI and the harness only need MHAPI_URL to switch between the twin
and the rig. Behind the API it simulates the printer's motion, the heated bed (fitted to the 2026-10-11
trial), the liquid in each well, egg-white clouding kinetics, and the Pi camera's view of each cell.

    python -m twin                       # serve on 127.0.0.1:8743, real time
    MHAPI_URL=http://127.0.0.1:8743 python -m mhapi.cli get_status
"""
DEFAULT_PORT = 8743
