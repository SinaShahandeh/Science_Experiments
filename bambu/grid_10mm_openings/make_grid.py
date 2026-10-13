from pathlib import Path
import json
import numpy as np
import trimesh
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

OUT = Path(__file__).parent
N, OPENING, WALL, HEIGHT = 12, 10, 3, 10
PITCH = OPENING + WALL
SIZE = N * PITCH + WALL
bars = []
for i in range(N + 1):
    c = i * PITCH + WALL / 2
    for dims, pos in [((WALL,SIZE,HEIGHT),(c,SIZE/2,HEIGHT/2)),
                      ((SIZE,WALL,HEIGHT),(SIZE/2,c,HEIGHT/2))]:
        bars.append(trimesh.creation.box(dims, trimesh.transformations.translation_matrix(pos)))
mesh = trimesh.boolean.union(bars, engine='manifold')
assert mesh.is_watertight and mesh.body_count == 1
assert np.allclose(mesh.extents, [159,159,10])
assert np.isclose(mesh.volume, (SIZE**2-N*N*OPENING**2)*HEIGHT)
mesh.export(OUT/'square_grid_159mm.stl')
fig=plt.figure(figsize=(10,8),facecolor='#f4f5f7')
ax=fig.add_subplot(111,projection='3d',facecolor='#f4f5f7')
light=np.array([-.3,-.5,1]);light/=np.linalg.norm(light)
shade=.55+.45*np.maximum(0, mesh.face_normals@light)
colors=np.c_[shade*.82,shade*.86,shade*.91,np.ones(len(shade))]
edges=sorted(set([0,SIZE]+[i*PITCH for i in range(N+1)]+[i*PITCH+WALL for i in range(N+1)]))
occupied=np.array([[i%2==0 or j%2==0 for j in range(25)] for i in range(25)])
polys=[]; shades=[]
for i in range(25):
    for j in range(25):
        if not occupied[i,j]: continue
        x,X=edges[i:i+2];y,Y=edges[j:j+2];h=HEIGHT
        polys.append([(x,y,h),(X,y,h),(X,Y,h),(x,Y,h)]); shades.append('#e6e9ed')
        for di,dj,face,color in [(-1,0,[(x,y,0),(x,Y,0),(x,Y,h),(x,y,h)],'#949da7'),(1,0,[(X,y,0),(X,Y,0),(X,Y,h),(X,y,h)],'#bbc2ca'),(0,-1,[(x,y,0),(X,y,0),(X,y,h),(x,y,h)],'#a3adb8'),(0,1,[(x,Y,0),(X,Y,0),(X,Y,h),(x,Y,h)],'#c4cbd2')]:
            ni,nj=i+di,j+dj
            if not (0<=ni<25 and 0<=nj<25) or not occupied[ni,nj]:
                polys.append(face);shades.append(color)
ax.add_collection3d(Poly3DCollection(polys,facecolors=shades,edgecolors='none',antialiased=False))
ax.set(xlim=(-5,164),ylim=(-5,164),zlim=(0,30));ax.set_box_aspect((169,169,45))
ax.view_init(elev=48,azim=-55);ax.set_axis_off()
fig.suptitle('12 × 12 square grid',fontsize=23,y=.93)
fig.text(.5,.10,'159 × 159 × 10 mm  •  10 mm clear openings  •  3 mm walls',ha='center',fontsize=13)
fig.savefig(OUT/'preview.png',dpi=180,bbox_inches='tight')
print(json.dumps({'size_mm':mesh.extents.tolist(),'cells':N*N,'watertight':mesh.is_watertight,'volume_cm3':mesh.volume/1000}))
