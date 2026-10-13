"""Open square lattice: 15x15 cells at 10 mm pitch, 3 mm walls, 10 mm tall."""
import trimesh, numpy as np
CELLS, PITCH, WALL, H = 15, 10.0, 3.0, 10.0
SIZE = CELLS * PITCH + WALL  # 153 mm outer
bars = []
for i in range(CELLS + 1):
    c = i * PITCH + WALL / 2
    bars.append(trimesh.creation.box((WALL, SIZE, H), trimesh.transformations.translation_matrix((c, SIZE/2, H/2))))
    bars.append(trimesh.creation.box((SIZE, WALL, H), trimesh.transformations.translation_matrix((SIZE/2, c, H/2))))
m = trimesh.boolean.union(bars, engine="manifold")
m.export("grid_15x15.stl")
print("extents", m.extents, "watertight", m.is_watertight, "volume cm3", m.volume/1000)
