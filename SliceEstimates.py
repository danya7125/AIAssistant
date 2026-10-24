"""Read engine estimates from fresh G-code headers, never invent missing fields."""
import math
import re


def extract(lines):
    result={'source':'fresh_slice_gcode_header','print_time_seconds':None,'filament_length_m':None,
            'actual_support_volume_mm3':None,'note':'Оценка движка для текущего результата, не измерение реальной печати.'}
    budget=262144
    for chunk in lines:
        if not isinstance(chunk,str): continue
        for line in chunk.splitlines():
            if line.startswith(';PREVIEW:'): continue
            budget-=len(line)
            if budget<0: return result
            if not line.startswith(';') and line.strip(): return result
            m=re.match(r';TIME:\s*(\d+(?:\.\d+)?)\s*$',line)
            if m:
                v=float(m.group(1))
                if math.isfinite(v):result['print_time_seconds']=v
            if line.startswith(';Filament used:'):
                lengths=[]
                for part in line.split(':',1)[1].split(','):
                    m=re.match(r'\s*(\d+(?:\.\d+)?)\s*(mm|m)\s*$',part)
                    if not m: lengths=[];break
                    value=float(m.group(1))/(1000 if m.group(2)=='mm' else 1)
                    if not math.isfinite(value):lengths=[];break
                    lengths.append(value)
                if lengths:
                    result['filament_length_m']=sum(lengths)
                    result['filament_by_extruder_m']=lengths
    return result
