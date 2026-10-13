"""Hardware-level simulator for mhapi-service (`mhapi-service --sim`): a simulated A1 behind the MQTT link
interface and a camera that renders the grid from the calibration, so the real service code (confirmed
motion, homing, safety layer, opacity reading) runs unchanged. Faults are injected with POST /sim/<action>."""
