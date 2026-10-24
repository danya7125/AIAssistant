"""Deterministic mesh metrics, not a strength or slicer simulation. No Qt calls."""
import math
import time
import numpy as np


def guard(cancel, deadline):
    if cancel is not None and cancel.is_set():
        raise RuntimeError('Анализ отменён.')
    if deadline is not None and time.monotonic() >= deadline:
        raise RuntimeError('Истёк срок анализа.')


def rotation(axis, degrees):
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    if axis == 'x':
        return np.array([[1,0,0],[0,c,-s],[0,s,c]])
    return np.array([[c,-s,0],[s,c,0],[0,0,1]])


def analyse(data, tensile_load_direction=None, critical_face_indices=None, cancel=None, deadline=None):
    guard(cancel, deadline)
    f = np.asarray(data['faces'], dtype=float)
    if f.ndim != 3 or f.shape[1:] != (3,3) or not len(f) or not np.isfinite(f).all():
        raise ValueError('Некорректная треугольная сетка.')
    if len(f)>500000:
        raise ValueError('Сетка слишком велика: максимум 500000 треугольников.')
    direction = None
    if tensile_load_direction is not None:
        direction = np.asarray(tensile_load_direction, dtype=float)
        if direction.shape != (3,) or not np.isfinite(direction).all() or np.linalg.norm(direction)<1e-12:
            raise ValueError('Нужно ненулевое конечное направление растяжения [X,Y,Z] в текущей системе сцены.')
        direction /= np.linalg.norm(direction)
    critical = None
    if critical_face_indices is not None:
        if not isinstance(critical_face_indices,list) or not 1<=len(critical_face_indices)<=200 or any(isinstance(i,bool) or not isinstance(i,int) or not 0<=i<len(f) for i in critical_face_indices):
            raise ValueError('critical_face_indices: от 1 до 200 действительных индексов граней.')
        critical = np.array(sorted(set(critical_face_indices)),dtype=int)
    cross = np.cross(f[:,1]-f[:,0], f[:,2]-f[:,0])
    twice_area = np.linalg.norm(cross,axis=1)
    area = twice_area/2
    good = twice_area>1e-12
    normals = cross/np.maximum(twice_area[:,None],1e-30)
    # Winding check: every quantised edge occurs twice with opposite directions.
    _,ix = np.unique(np.round(f.reshape(-1,3)/1e-5).astype(np.int64),axis=0,return_inverse=True)
    ids=ix.reshape(-1,3)
    edges=np.concatenate([ids[:,[0,1]],ids[:,[1,2]],ids[:,[2,0]]])
    _,inv,counts=np.unique(np.sort(edges,axis=1),axis=0,return_inverse=True,return_counts=True)
    balance=np.bincount(inv,weights=np.where(edges[:,0]<edges[:,1],1,-1))
    closed=bool(np.all(counts==2) and np.all(balance==0) and good.all())
    # Disconnected shells can have different winding: do not infer outward normals for them.
    parent=np.arange(len(f),dtype=np.int64)
    def root(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=parent[i]
        return i
    order=np.argsort(inv)
    if closed:
        face_ids=np.tile(np.arange(len(f)),3)
        for k in range(0,len(order),2):
            if k%12000==0: guard(cancel,deadline)
            a,b=root(face_ids[order[k]]),root(face_ids[order[k+1]])
            parent[a]=b
        connected=len({root(i) for i in range(len(f))})==1
    else:
        connected=False
    centered=f-f.reshape(-1,3).mean(0)
    volume=float(np.einsum('ij,ij->i',centered[:,0],np.cross(centered[:,1],centered[:,2])).sum()/6)
    component_volumes=[]
    if closed:
        roots=np.array([root(i) for i in range(len(f))])
        signed=np.einsum('ij,ij->i',centered[:,0],np.cross(centered[:,1],centered[:,2]))/6
        for key in np.unique(roots):
            component_volumes.append(float(signed[roots==key].sum()))
    # Uniform shell winding can be used as a geometric assumption. Mixed winding
    # may encode cavities or errors and is deliberately not repaired automatically.
    consistent_shells=bool(component_volumes) and (all(v>1e-9 for v in component_volumes) or all(v<-1e-9 for v in component_volumes))
    trusted=closed and consistent_shells
    if volume<0: normals=-normals
    layer=data.get('layer_height')
    first=data.get('layer_height_0')
    angle=data.get('support_angle')
    layer=float(layer) if isinstance(layer,(int,float)) and math.isfinite(layer) and layer>0 else None
    first=float(first) if isinstance(first,(int,float)) and math.isfinite(first) and first>0 else layer
    angle=float(angle) if isinstance(angle,(int,float)) and math.isfinite(angle) and 0<=angle<=90 else None
    specs=[('x',0),('x',90),('x',-90),('x',180),('z',90),('z',-90)]
    specs += [(a,d*sign) for a in ('x','z') for d in (15,30,45,60,75) for sign in (1,-1)]
    candidates=[]
    total=float(area.sum())
    for axis,deg in specs:
        guard(cancel,deadline)
        r=rotation(axis,deg)
        w=f.dot(r.T)
        lo=w.min((0,1));hi=w.max((0,1))
        height=float(hi[1]-lo[1])
        ys=w[:,:,1]-lo[1]
        contact_mask=np.all(ys<=.02,axis=1)
        ny=normals.dot(r.T)[:,1]
        inclined=(np.abs(ny)>1e-6)&(np.abs(ny)<1-1e-6)&good
        stair=layer*np.abs(ny) if layer is not None else None
        critical_area=float(area[critical].sum()) if critical is not None else None
        critical_stair=None
        if critical is not None and stair is not None and critical_area>0:
            critical_stair=float(np.sum(area[critical]*stair[critical]*inclined[critical])/critical_area)
        down=(ny < -math.sin(math.radians(angle))-1e-9) & ~contact_mask & good if angle is not None and trusted else None
        support_area=float(area[down].sum()) if down is not None else None
        # Integral of each downward triangle's projection times height above bed.
        # This deliberately counts overlaps and material beneath; NOT actual support volume.
        prism=float(np.sum(area[down]*(-ny[down])*ys[down].mean(1))) if down is not None else None
        load_fraction=abs(float(direction.dot(r.T)[1])) if direction is not None else None
        size=(hi-lo).tolist()
        bounds=data.get('build_size')
        candidates.append({'axis':axis,'degrees':deg,'height_mm':height,'size_xyz_y_up_mm':size,
            'fits_build_box':bool(np.all(hi-lo<=np.asarray(bounds)+.001)) if bounds is not None else None,
            'planar_contact_mm2':float(area[contact_mask].sum()),
            'overhang_area_mm2':support_area,
            'support_to_plate_prism_sum_mm3':prism,
            'actual_support_volume_mm3':None,
            'inclined_surface_area_mm2':float(area[inclined].sum()),
            'inclined_surface_layer_quantization_proxy_mm':float(np.sum(area[inclined]*stair[inclined])/area[inclined].sum()) if stair is not None and np.any(inclined) else (0.0 if stair is not None else None),
            'critical_surface_proxy_mm':critical_stair,
            'critical_surface_area_mm2':critical_area,
            'tensile_direction_normal_to_layers_fraction':load_fraction,
            'strength_safety_factor':None,
            'geometric_layer_count':int(1+math.ceil(max(0,height-first)/layer)) if layer is not None else None,
            'print_time_seconds':None,'filament_length_m':None})
    # No arbitrary aggregate score. Report tradeoffs as a Pareto frontier.
    metrics=['height_mm','inclined_surface_layer_quantization_proxy_mm']
    if trusted and angle is not None: metrics.append('support_to_plate_prism_sum_mm3')
    if critical is not None: metrics.append('critical_surface_proxy_mm')
    if direction is not None: metrics.append('tensile_direction_normal_to_layers_fraction')
    def vector(c):
        return [c[k] for k in metrics if c[k] is not None]+[-c['planar_contact_mm2']]
    eligible=[c for c in candidates if c['fits_build_box'] is not False]
    for c in candidates:
        v=vector(c)
        c['pareto_candidate']=c in eligible and not any(all(x<=y+1e-9 for x,y in zip(vector(b),v)) and any(x<y-1e-9 for x,y in zip(vector(b),v)) for b in eligible)
    candidates.sort(key=lambda c:(c['fits_build_box'] is False,not c['pareto_candidate'],-c['planar_contact_mm2'],c['height_mm']))
    return {'model_id':data['model_id'],'candidate_count':len(candidates),'candidates':candidates,
        'inputs':{'layer_height_mm':layer,'first_layer_height_mm':first,'support_angle_from_vertical_deg':angle,'contact_tolerance_mm':.02,'tensile_direction_source':'caller_supplied' if direction is not None else None,'critical_faces_source':'caller_supplied' if critical is not None else None},
        'mesh_checks':{'closed_consistent_edges':closed,'single_connected_shell':connected,'shell_count':len(component_volumes),'shell_winding_consistent':consistent_shells,'normals_usable_for_overhang':trusted,'self_intersections_checked':False,'shell_intersections_or_nesting_checked':False},
        'method':{'support':'Sum of projected downward triangle prisms down to bed. Overlapping prisms and solid beneath are NOT subtracted. Geometric comparison proxy, not real support volume or material consumption; bridges and support algorithms not simulated.',
            'surface':'h*abs(normal_y) on inclined facets, area-weighted. Layer quantization proxy, NOT Ra/Rz, dimensional accuracy or tolerances. Horizontal/vertical ideal planes excluded.',
            'load':'Absolute component of explicitly supplied tensile direction along layer normal (Y); 0=in-plane, 1=normal. No stress field, bending/torsion or strength calculation.',
            'ranking':'Pareto tradeoffs: lower height/support proxy/surface proxy/load-normal fraction and higher contact. No universal optimum. Bounds exclude forbidden areas; actual placement checks those separately.'},
        'not_calculated':{'strength':'Requires loads, constraints and measured anisotropic printed-material properties; not inferred from material name.',
            'critical_surfaces':None if critical is not None else 'Critical faces not identified; whole-surface proxy only.',
            'time_and_material':'Only the slicing engine can estimate these for actual settings; candidate values remain null until individually sliced.',
            'actual_supports':'Requires actual slicer support toolpaths.',
            'search':'26 single-axis orientations, not exhaustive arbitrary 3D rotations.',
            '5d':'Not evaluated.'}}
