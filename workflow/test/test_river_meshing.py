import workflow.river_tree
import shapely
import numpy as np
from matplotlib import pyplot as plt

def y_with_extension():
    points = [[(1,0), (0,0)],
              [(1,1), (1,0)],
              [(1,-1),(1,0)],
              [(2,-1),(1,-1)]]
    return shapely.geometry.MultiLineString(points)

y = y_with_extension()
box = shapely.geometry.Polygon([(-3,-3), (3,-3), (3,3), (-3,3)])
sbox = workflow.split_hucs.SplitHUCs([box,])
rivers = workflow.simplify_and_prune(sbox,y, simplify=0)
river = rivers[0]

corr1,corr2,corr3 = workflow.river_tree.create_river_corridor(river, 0.04)
corr = corr3

fig = plt.figure()
ax = fig.add_subplot(111)
ax.plot(sbox.exterior().exterior.xy[0], sbox.exterior().exterior.xy[1], 'k')
ax.plot(corr.exterior.xy[0], corr.exterior.xy[1], 'b-x')
for reach in rivers[0].dfs():
    ax.plot(reach.xy[0], reach.xy[1], 'r-x')

conn = workflow.river_tree.to_quads(river)
for elem in conn:
    looped_conn = elem[:]
    looped_conn.append(elem[0])
    coords = np.array([corr.exterior.coords[n] for n in looped_conn])
    ax.plot(coords[:,0], coords[:,1], 'm-^')

plt.show()
