from pathlib import Path
import json
import numpy as np
import trimesh
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

OUT=Path(__file__).parent
PITCH,WALL,H=12,2,10
# Coordinates on the original 12-cell lattice. One cell inset at each corner.
GROUPS=[('center',4,4,4),('front_left',1,1,2),('front_right',9,1,2),
        ('back_left',1,9,2),('back_right',9,9,2)]
meshes=[];polys=[];colors=[]
for name,ix,iy,n in GROUPS:
    size=n*PITCH+WALL; ox=ix*13+(55-size)/2 if name=="center" else ix*13+1.5;oy=iy*13+(55-size)/2 if name=="center" else iy*13+1.5
    bars=[]
    for i in range(n+1):
        c=i*PITCH+WALL/2
        for dims,pos in [((WALL,size,H),(ox+c,oy+size/2,H/2)),((size,WALL,H),(ox+size/2,oy+c,H/2))]:
            bars.append(trimesh.creation.box(dims,trimesh.transformations.translation_matrix(pos)))
    m=trimesh.boolean.union(bars,engine='manifold')
    assert m.is_watertight and m.body_count==1
    assert np.isclose(m.volume,(size*size-n*n*100)*H)
    m.export(OUT/(name+'.stl'));meshes.append(m)
    edges=sorted(set([0,size]+[i*PITCH for i in range(n+1)]+[i*PITCH+WALL for i in range(n+1)]))
    count=2*n+1
    occ=np.array([[i%2==0 or j%2==0 for j in range(count)] for i in range(count)])
    for i in range(count):
        for j in range(count):
            if not occ[i,j]:continue
            x,X=np.array(edges[i:i+2])+ox;y,Y=np.array(edges[j:j+2])+oy
            polys.append([(x,y,H),(X,y,H),(X,Y,H),(x,Y,H)]);colors.append('#e6e9ed')
            for di,dj,face,color in [(-1,0,[(x,y,0),(x,Y,0),(x,Y,H),(x,y,H)],'#949da7'),(1,0,[(X,y,0),(X,Y,0),(X,Y,H),(X,y,H)],'#bbc2ca'),(0,-1,[(x,y,0),(X,y,0),(X,y,H),(x,y,H)],'#a3adb8'),(0,1,[(x,Y,0),(X,Y,0),(X,Y,H),(x,Y,H)],'#c4cbd2')]:
                ni,nj=i+di,j+dj
                if not(0<=ni<count and 0<=nj<count) or not occ[ni,nj]:polys.append(face);colors.append(color)
combined=trimesh.util.concatenate(meshes)
assert combined.is_watertight and combined.body_count==5
assert np.allclose(combined.extents,[130,130,10])
combined.export(OUT/'open_grid_2mm.stl')
fig=plt.figure(figsize=(10,8),facecolor='#f4f5f7');ax=fig.add_subplot(111,projection='3d',facecolor='#f4f5f7')
ax.add_collection3d(Poly3DCollection(polys,facecolors=colors,edgecolors='none',antialiased=False))
ax.set(xlim=(0,159),ylim=(0,159),zlim=(0,30));ax.set_box_aspect((159,159,40));ax.view_init(elev=58,azim=-60);ax.set_axis_off()
fig.suptitle('Five open-bottom grid islands',fontsize=23,y=.93)
fig.text(.5,.105,'Center: 4 × 4  •  Corners: 2 × 2 each',ha='center',fontsize=15)
fig.text(.5,.06,'10 mm openings  •  2 mm walls  •  10 mm height — no floors',ha='center',fontsize=13)
fig.savefig(OUT/'preview.png',dpi=150,bbox_inches='tight')
print(json.dumps({'islands':combined.body_count,'openings':32,'overall_extents_mm':combined.extents.tolist(),'volume_cm3':combined.volume/1000}))
