"""Module for working with tree data structures, built on watershed_workflow.tinytree"""
import logging
import collections
import numpy as np
import copy
import fiona
import pandas as pd
import itertools

import shapely.geometry
import shapely.ops

import watershed_workflow.utils
import watershed_workflow.tinytree


_tol = 1.e-7

class RiverTree(watershed_workflow.tinytree.Tree):
    """A tree node data structure"""
    def __init__(self, segment=None, properties=None, children=None):
        super(RiverTree, self).__init__(children)
        self.segment = segment

        assert(properties is None)
        # assert(hasattr(self.segment, 'properties'))
        if properties is not None:
            self.properties = properties
        elif hasattr(self.segment, 'properties'):
            self.properties = self.segment.properties
        else:
            self.properties = dict()

    def addChild(self, segment):
        if type(segment) is RiverTree:
            super(RiverTree,self).addChild(segment)
        else:
            super(RiverTree,self).addChild(type(self)(segment))
        return self.children[-1]

    def dfs(self):
        for node in self.preOrder():
            if node.segment is not None:
                yield node.segment

    def leaf_nodes(self):
        """Generator for all leaves of the tree."""
        for it in self.preOrder():
            if len(it.children) == 0 and it.segment is not None:
                yield it

    def leaves(self):
        """Generator for all leaves of the tree."""
        for n in self.leaf_nodes():
            yield n.segment

    def __len__(self):
        # kinda hacky way of getting the count
        return sum(1 for i in self.dfs())

    def __iter__(self):
        return self.dfs()

    def check_child_consistency(self, tol=1.e-8):
        for child in self.children:
            if not watershed_workflow.utils.close(child.segment.coords[-1], self.segment.coords[0]):
                return False
        return True

    def get_inconsistent(self, tol=1.e-8):
        inconsistent = []
        for child in self.children:
            if not watershed_workflow.utils.close(child.segment.coords[-1], self.segment.coords[0]):
                logging.warning("  INCONSISTENT:")
                logging.warning("    child: %r"%(child.segment.coords[:]))
                logging.warning("    parent: %r"%(self.segment.coords[:]))
                inconsistent.append(child)
        return inconsistent

    def deep_copy(self):
        cp = copy.deepcopy(self)
        for seg1,seg2 in zip(cp,self):
            seg1.properties = copy.deepcopy(seg2.properties)
        return cp

    def shallow_copy(self):
        cp = copy.copy(self)
        for seg1,seg2 in zip(cp,self):
            seg1.properties = copy.copy(seg2.properties)
        return cp
    
            
def _get_matches(seg, segments, segment_found):
    """Find segments attached to seg amongst those not already found"""
    matches = [i for i in range(len(segments)) if not segment_found[i]
               and watershed_workflow.utils.close(segments[i].coords[-1], seg.coords[0])]
    segment_found[matches] = True
    return matches

def _go(i_seg, tree, segments, segments_found):
    """Recursive helper function for generating a tree based on a matching function."""
    count = 0
    tree.addChild(segments[i_seg])
    for m in _get_matches(segments[i_seg], segments, segments_found):
        count += _go(m, tree, segments, segments_found)
    return count + 1

def make_trees(segments):
    """Forms tree(s) from a list of segments."""
    logging.debug("Generating trees")
    endpoint_indices = find_endpoints(segments)
    logging.debug("  found: %i outlets"%len(endpoint_indices))
    for endp in endpoint_indices:
        logging.debug("    at: %r"%list(segments[endp].coords[-1]))

    # check if any endpoint lives on another segment
    segs_to_remove = []
    segs_to_add = []
    for endpoint_index in endpoint_indices:
        endpoint_seg = shapely.geometry.LineString(segments[endpoint_index].coords[-2:])
        try:
            inter = next(i for i,seg in enumerate(segments)
                         if endpoint_seg.intersects(seg)
                         and i != endpoint_index
                         and watershed_workflow.utils.close(endpoint_seg.intersection(seg).coords[0], endpoint_seg.coords[-1], 1.e-5))
        except StopIteration:
            logging.debug("   outlet %i is not faux"%endpoint_index)
        else:
            logging.debug("   faux outlet: %i segment: %i"%(endpoint_index, inter))
            segs_to_remove.append(inter)
            
            print("splitting segment: %r"%list(segments[inter].coords))
            print("   at: %r"%list(segments[endpoint_index].coords[-1]))
            segs_to_add.extend(watershed_workflow.utils.cut(segments[inter], endpoint_seg))
            
    if len(segs_to_remove) != 0:
        segments = list(segments)
        for i in sorted(segs_to_remove, reverse=True):
            segments.pop(i)
        segments.extend(segs_to_add)
        segments = shapely.geometry.MultiLineString(segments)

        # regenerate endpoints (indicies may have changed)
        endpoint_indices = find_endpoints(segments)
        logging.debug("  found: %i outlets"%len(endpoint_indices))

    # generate all trees
    segment_found = np.zeros((len(segments),), bool)
    gcount = 0
    trees = []
    for endpoint_index in endpoint_indices:
        tree = RiverTree()
        gcount += _go(endpoint_index, tree, segments, segment_found)
        trees.append(tree)
    assert(gcount == len(segments))
    return trees

def find_endpoints(segments):
    """Finds a list of indices of all segments whose endpoint is not a beginpoint.

    Note these may truely be the tree root, or they may end at a
    midpoint on another segment (mistake in input data).
    """
    endpoints = []
    for i,s in enumerate(segments):
        c = s.coords[-1]
        try:
            next(s2 for s2 in segments if watershed_workflow.utils.close(s2.coords[0], c))
        except StopIteration:
            endpoints.append(i)
    return endpoints

def tree_to_list(tree):
    return shapely.geometry.MultiLineString(list(tree.dfs()))

def forest_to_list(forest):
    """A forest is a list of trees.  Returns a flattened list of trees."""
    return shapely.geometry.MultiLineString([r for tree in forest for r in tree.dfs()])

def is_consistent(tree, tol=1.e-8):
    """Checks the geometric consistency of the tree."""
    return not any(not node.check_child_consistency(tol) for node in tree.preOrder())

def get_inconsistent(tree, tol=1.e-8):
    """Gets a list of inconsistent nodes of the tree."""
    return [n for node in tree.preOrder() for n in node.get_inconsistent(tol)]

def sort_children_by_angle(tree, reverse=False):
    """Sorts the children of a given segment by their angle with respect to that segment."""
    for node in tree.preOrder():
        if len(node.children) > 1:
            # compute tangents
            my_seg_tan = np.array(node.segment.coords[0]) - np.array(node.segment.coords[1])

            if reverse:
                def angle(c):
                    tan = np.array(c.segment.coords[-2]) - np.array(c.segment.coords[-1])
                    return -watershed_workflow.utils.angle(my_seg_tan, tan)
            else:
                def angle(c):
                    tan = np.array(c.segment.coords[-2]) - np.array(c.segment.coords[-1])
                    return watershed_workflow.utils.angle(my_seg_tan, tan)

            node.children.sort(key=angle)

def create_river_mesh(river, watershed=None,widths=8, junction_treatment=True):

    if type(widths)== dict:
        dilation_width=np.mean(list(widths.values()))
    else:
        dilation_width=widths
    
    # creating a polygon for river corridor by dilating the river tree
    corr = create_river_corridor(river, dilation_width)

    # defining special elements in the mesh
    elems= to_quads(river, dilation_width, watershed, corr)

    # setting river_widths in the river corridor polygon
    corr_new= set_river_width(river,corr, widths=widths,junction_treatment=junction_treatment)

    # treating non-convexity at junctions
    if junction_treatment:
        corr_new=junction_treatment_by_nudge(river, corr_new)

    return elems, corr_new


def create_river_corridor(river, river_width):
    """Returns a polygon representing the river corridor."""
    # first sort the river so that in a search we always take paddlers right...
    sort_children_by_angle(river, True)
    delta = river_width / 2.

    # buffer by the width
    mls = shapely.geometry.MultiLineString([r for r in river.dfs()])
    corr = mls.buffer(delta, cap_style=shapely.geometry.CAP_STYLE.flat,
                      join_style=shapely.geometry.JOIN_STYLE.mitre)

    # cycle the corridor points to start and end with the 1st point...
    corr_p = list(corr.exterior.coords[:-1])
    outlet_p = river.segment.coords[-1]
    index_min = min(range(len(corr_p)), key=lambda i : watershed_workflow.utils.distance(corr_p[i], outlet_p))
    plus_one = (index_min+1)%len(corr_p)
    minus_one = (index_min-1)%len(corr_p)
    if (watershed_workflow.utils.distance(corr_p[plus_one], outlet_p) < watershed_workflow.utils.distance(corr_p[minus_one], outlet_p)):
        corr2_p = corr_p[plus_one:]+corr_p[0:plus_one]
    else:
        corr2_p = corr_p[index_min:]+corr_p[0:index_min]
    corr2 = shapely.geometry.Polygon(corr2_p)

    # remove endpoint-doubles that we want to be a single point and
    # weird artifact triples at junctions
    corr3_p = []
    i = 0
    while i < len(corr2_p):
        logging.debug(f'considering {i}')
        if i == 0 or i == len(corr2_p)-1:
            # keep first and last always -- first two points make the outlet segment
            logging.debug(f' always keeping')
            corr3_p.append(corr2_p[i])
        else:
            if watershed_workflow.utils.distance(corr2_p[i-1], corr2_p[i]) < 3*delta:
                # is this a triple point?
                if watershed_workflow.utils.distance(corr2_p[i+1], corr2_p[i]) < 3*delta:
                    logging.debug(' triple point!')
                    # triple point, average neighbors and skip the next point
                    corr3_p.append(watershed_workflow.utils.midpoint(corr2_p[i+1], corr2_p[i-1]))
                    i += 1
                else:
                    # double point -- an end of a first order stream
                    logging.debug(' double point')
                    corr3_p.append(watershed_workflow.utils.midpoint(corr2_p[i-1], corr2_p[i]))
            else:
                # will the next point deal with this?
                if watershed_workflow.utils.distance(corr2_p[i], corr2_p[i+1]) < 3*delta:
                    logging.debug(' not my problem')
                    pass
                else:
                    logging.debug(' keeping')
                    corr3_p.append(corr2_p[i])
        i += 1

    # create the polgyon
    corr3 = shapely.geometry.Polygon(corr3_p)
    return corr3


def to_quads(river, delta, huc, corr,ax=None):
    """Iterate over the rivers, creating quads and pentagons forming the corridor."""
    
    coords=corr.exterior.coords[:-1]
    # number the nodes in a dfs pattern, creating empty space for elements
    for i, node in enumerate(river.preOrder()):
        node.id = i
        node.elements = [list() for l in range(len(node.segment.coords)-1)]
        assert(len(node.elements) >= 1)
        node.touched = 0

    import time
    def pause():
        time.sleep(0.)
        
    # iterate over the tree in an out-and-back-and-in-between
    # traversal, where every node appears num_children + 1 times,
    # before and after and between each child.    
    ic = 0
    total_touches = 0
    for node in river.prePostInBetweenOrder():
        logging.debug(f'touching {node.id} (previously touched {node.touched} times with {len(node.children)} children)')
        if node.touched == 0:
            logging.debug(f'  first time around! {node.touched+1}')
            # not yet touched -- add the first coordinates
            seg_coords = [coords[ic],]
            for j in range(len(node.elements)):
                node.elements[j].append(ic)
                ic += 1
                node.elements[j].append(ic)
                seg_coords.append(coords[ic])

            node.touched += 1
            total_touches += 1

            # plot it...
            seg_coords = np.array(seg_coords)
            #ax.plot(seg_coords[:,0], seg_coords[:,1], 'm^')
            pause()

        elif node.touched == 1 and len(node.children) == 0:
            # leaf node, last time
            logging.debug(f' last time around a leaf! {node.touched+1}')
            # increment to avoid double-counting the point in the triangle on the ends
            seg_coords = [coords[ic],]
            ic += 1
            node.elements[-1].append(ic)
            seg_coords.append(coords[ic])
            for j in reversed(range(len(node.elements)-1)):
                node.elements[j].append(ic)
                ic += 1
                node.elements[j].append(ic)
                seg_coords.append(coords[ic])
            node.touched += 1
            total_touches += 1

            # plot it...
            seg_coords = np.array(seg_coords)
            #ax.plot(seg_coords[:,0], seg_coords[:,1], 'm^')

            # also plot the conn
            for i, elem in enumerate(node.elements):
                looped_conn = elem[:]
                looped_conn.append(elem[0])
                if i == len(node.elements)-1:
                    assert(len(looped_conn) == 4)
                else:
                    assert(len(looped_conn) == 5)
                cc = np.array([coords[n] for n in looped_conn])
                #ax.plot(cc[:,0], cc[:,1], 'g-o')
            pause()
            

        elif node.touched == len(node.children):
            logging.debug(f'  last time around! {node.touched+1}')
            seg_coords = [coords[ic],]
            # touched enough times that this is the last appearance
            # add the last coordinates
            for j in reversed(range(len(node.elements))):
                node.elements[j].append(ic)
                ic += 1
                node.elements[j].append(ic)
                seg_coords.append(coords[ic])
            node.touched += 1
            total_touches += 1

            # plot it...
            seg_coords = np.array(seg_coords)
            #ax.plot(seg_coords[:,0], seg_coords[:,1], 'm^')

            # also plot the conn
            for i,elem in enumerate(node.elements):
                looped_conn = elem[:]
                looped_conn.append(elem[0])
                if i == len(node.elements)-1:
                    assert(len(looped_conn) == (node.touched+3))
                else:
                    assert(len(looped_conn) == 5)
                cc = np.array([coords[n] for n in looped_conn])
                for c in cc:
                    # note, the more acute an angle, the bigger this distance can get...
                    # so it is a bit hard to pin this multiple down -- using 5 seems ok?
                    assert(watershed_workflow.utils.close(tuple(c), node.segment.coords[len(node.segment.coords)-(i+1)], 5*delta) or \
                           watershed_workflow.utils.close(tuple(c), node.segment.coords[len(node.segment.coords)-(i+2)], 5*delta))
                           
                           
                #ax.plot(cc[:,0], cc[:,1], 'g-o')
            pause()
            
            
        else:
            logging.debug(f'  middle time around! {node.touched+1}')
            assert(node.touched < len(node.children))
            # touched in between children
            # therefore this is at least a pentagon
            # add the middle node on the last element
            node.elements[-1].append(ic)
            node.touched += 1

            #ax.scatter([coords[ic][0],], [coords[ic][1],], c='m', marker='^')
            pause()

    assert(len(coords) == (ic+1))
    assert(len(river)*2 == total_touches)
    elems=[el for node in river.preOrder() for el in node.elements]
    ## ensuring that the pentagons at the junctions are convex (this shifts pentagon upstream, we don't want this)
    # elems=junction_treatment(elems, coords,junction_option)
    # # note that after junction_treatment1 node.elemements and m2d.conn may not be exactly same
    # i=0
    # for node in river.preOrder():
    #     for j, el in enumerate(node.elements):
    #         node.elements[j] = elems[i]
    #         i+=1
    # assert([el for node in river.preOrder() for el in node.elements]==elems)
    return elems

def set_river_width(river,corr, widths=8, junction_treatment=None):
    corr_coords=corr.exterior.coords[:-1]
    for j,node in enumerate(river.preOrder()):
        order=node.properties["StreamOrder"]

        if type(widths)== dict:
            target_width=width_cal(widths,order)
        else:
            target_width=widths

        for i, elem in enumerate(node.elements): # treating the upstream edge of the element
            if len(elem)==4:
                p1=np.array(corr_coords[elem[1]][:2]) # points of the upstream edge of the quad
                p2=np.array(corr_coords[elem[2]][:2])
                [p1_, p2_]= move_to_target_separation(p1,p2,target_width)
                corr_coords[elem[1]]=tuple(p1_)
                corr_coords[elem[2]]=tuple(p2_)

            if len(elem)==5:
                p1=np.array(corr_coords[elem[1]][:2]) # neck of the pent
                p2=np.array(corr_coords[elem[3]][:2])
                [p1_, p2_]= move_to_target_separation(p1,p2,target_width)
                corr_coords[elem[1]]=tuple(p1_)
                corr_coords[elem[3]]=tuple(p2_)
                
            if i==0: # this is to treat the most downstream edge which is left out so far
                p1=np.array(corr_coords[elem[0]][:2]) # points of the upstream edge of the quad/pent
                p2=np.array(corr_coords[elem[-1]][:2])
                [p1_, p2_]= move_to_target_separation(p1,p2,target_width)
                corr_coords[elem[0]]=tuple(p1_)
                corr_coords[elem[-1]]=tuple(p2_)

    corr_coords_new=corr_coords+[corr_coords[0]]
    return shapely.geometry.Polygon(corr_coords_new)


def move_to_target_separation(p1,p2, target):
    import math
    d_vec=p1-p2 # separation vector
    d=np.sqrt(d_vec.dot(d_vec)) # distance
    delta= target-d
    p1_=p1+0.5*delta*(d_vec)/d
    p2_=p2-0.5*delta*(d_vec)/d
    d_=watershed_workflow.utils.distance(p1_,p2_)
    assert(math.isclose(d_,target, rel_tol=1e-5))
    return [p1_, p2_]

def width_cal(width_dict,order):
    if order > max(width_dict.keys()):
        width=width_dict[max(width_dict.keys())]
    elif order < min(width_dict.keys()):
        width=width_dict[min(width_dict.keys())]
    else: 
         width=width_dict[order]
    return width

def junction_treatment_by_nudge(river, corr):

    coords=corr.exterior.coords[:-1]

    for j, node in enumerate(river.preOrder()):
        for elem in node.elements:
                if len(elem)==5: # checking and treating this pentagon
                    points=[coords[id] for id in elem];  i=0
                    points_orig=copy.deepcopy(points)
                    while not watershed_workflow.utils.isConvex(points):
                        p1, p3= [np.array(points[1]), np.array(points[3])]
                        d=p1-p3
                        p1_=p3+1.01*d
                        p3_=p1-1.01*d
                        points[1]=tuple(p1_)
                        points[3]=tuple(p3_)
                        i+=1
                    logging.debug(" pent in node", j,"was adjusted", i, " times")
                    # since we shifted both points 1 and 3 howver only one was required to be moved
                    # we put one of them to their oroginal location to see if that was a irrelevant shift
                    if not watershed_workflow.utils.isConvex(points_orig):
                        points_new=copy.deepcopy(points)
                        points_new[1]=points_orig[1]
                        if watershed_workflow.utils.isConvex(points_new):
                            logging.debug("Undoing nudge of 1st neck point kept the pentagon convex")
                        else:
                            logging.debug("Undoing nudge of 1st neck point made pentagon non-convex, trying second next point")
                            points_new=copy.deepcopy(points)
                            points_new[3]=points_orig[3]

                        assert(watershed_workflow.utils.isConvex(points_new))
                        
                        # updating coords
                        for id, point in zip(elem,points_new):
                            coords[id]=point    

    corr_coords_new=coords+[coords[0]]                     
    return shapely.geometry.Polygon(corr_coords_new)
                        


#def junction_treatment_by_elem_redefine(elems, coords,junction_option='all_pentagons'): # not sure if we still need this
#     """Iterate over pentagon elements, check for convexity and treat non-convexity 
#     by either using triangle at the junction or making one of the upstream point as triangle"""
#     elem_lens=[len(elem) for elem in elems]
#     pents=np.where(np.array(elem_lens)==5)[0]
#     tri_count=0
#     for pent in pents:
#         elem=elems[pent]
#         points=[coords[i] for i in elem]
#         if junction_option=='all_tris':
#             elems[pent]=[elem[i] for i in [0,1,3,4]]
#             tri=[elem[i] for i in [1,2,3]]
#             elems.append(tri) 
#         elif junction_option=='tris_at_convex':
#             if not watershed_workflow.utils.isConvex(points):
#                 elems[pent]=[elem[i] for i in [0,1,3,4]] # made the DS element quad
#                 tri=[elem[i] for i in [1,2,3]] # defined traingle for the junction
#                 elems.append(tri)
#         elif junction_option=='all_pentagons':
#             if not watershed_workflow.utils.isConvex(points):
#                 elems[pent]=[elem[i] for i in [0,1,3,4]]

#                 # making upstream **branch 1** as pentagon
#                 logging.debug(f'attemping to create convex pentagon on branch 1')
#                 pent_up=[elem[1],elem[1]+1,elem[2]-1,elem[2],elem[3]]# making one branch as pengaton
#                 # check convexity
#                 points_new=[coords[i] for i in pent_up]
#                 if watershed_workflow.utils.isConvex(points_new):
#                     logging.debug(f'branch 1 converted into a convex pentagon')
#                     ind_to_replace=elems.index(pent_up[:-1])
#                     elems[ind_to_replace]=pent_up
#                 else:
#                     logging.debug(f'pentagon in branch 1 is not convex, now trying branch 2')
#                     pent_up=[elem[2],elem[2]+1,elem[3]-1,elem[3],elem[1]]# making one branch as pengaton
#                     points_new=[coords[i] for i in pent_up]
#                     if watershed_workflow.utils.isConvex(points_new):
#                         logging.debug(f'branch 2 converted into a convex pentagon')
#                         ind_to_replace=elems.index(pent_up[:-1])
#                         elems[ind_to_replace]=pent_up
#                     else:
#                         logging.debug(f'failed to create convex polygon at the junction, adding a traingle')
#                         tri=[elem[i] for i in [1,2,3]]
#                         elems.append(tri)
#                         tri_count=+1
#         else:
#             print('junction_option not valid')
#     if tri_count>0:                    
#         print('Warning: ',tri_count," triangles introduced at junctions" )                    
#     return elems
                    

