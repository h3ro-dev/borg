"""Generate the site's original circuit cube. Python stdlib; deterministic output."""
from pathlib import Path
import random
r = random.Random(73)
out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 660" fill="none">',
       '<defs><radialGradient id="halo"><stop stop-color="#94d93c" stop-opacity=".09"/><stop offset="1" stop-color="#94d93c" stop-opacity="0"/></radialGradient></defs>',
       '<circle cx="355" cy="330" r="320" fill="url(#halo)"/>']
# Sparse engineering guides. These are decorative, not telemetry.
for x,y in [(65,115),(606,115),(65,530),(606,530)]:
    out.append(f'<path d="M{x-6} {y}h12m-6-6v12" stroke="#526047" stroke-width=".8"/>')
out.append('<path d="M49 450 329 611 641 432M51 244 331 82 627 253" stroke="#3b4b2d" stroke-dasharray="2 8"/>')
# Isometric faces, subdivided into unique panel modules.
faces = [((338,90),(-226,128),(226,128),['#28351e','#303d23','#222e1b','#36472a']),
         ((112,218),(226,128),(0,259),['#1b2517','#22301c','#172013','#2b3b21']),
         ((338,346),(226,-128),(0,259),['#111a0e','#182312','#1d2b14','#24341b'])]
def point(o,u,v,x,y): return (o[0]+u[0]*x+v[0]*y,o[1]+u[1]*x+v[1]*y)
def pstr(points): return ' '.join(f'{x:.2f},{y:.2f}' for x,y in points)
for face,(o,u,v,palette) in enumerate(faces):
    out.append(f'<polygon points="{pstr([point(o,u,v,0,0),point(o,u,v,1,0),point(o,u,v,1,1),point(o,u,v,0,1)])}" fill="#0b1009" stroke="#718f49" stroke-width="1.3"/>')
    n=12
    for j in range(n):
        for i in range(n):
            x,y=(i+.10)/n,(j+.10)/n
            w,h=.80/n,.80/n
            vertices=[point(o,u,v,x,y),point(o,u,v,x+w,y),point(o,u,v,x+w,y+h),point(o,u,v,x,y+h)]
            fill=r.choice(palette)
            if r.random()<.038: fill=r.choice(['#a5ea43','#88bd37','#55792d'])
            out.append(f'<polygon points="{pstr(vertices)}" fill="{fill}" stroke="#526a39" stroke-opacity=".50" stroke-width=".5"/>')
            if r.random()<.55:
                inset=.17/n
                a=point(o,u,v,x+inset,y+inset)
                b=point(o,u,v,x+w-inset,y+inset)
                c=point(o,u,v,x+w-inset,y+h-inset)
                out.append(f'<polyline points="{pstr([a,b,c])}" stroke="#7a9d4d" stroke-opacity=".40" stroke-width=".65"/>')
            if r.random()<.23:
                for k in range(3):
                    a=point(o,u,v,x+.2/n,y+(.25+k*.15)/n)
                    b=point(o,u,v,x+.6/n,y+(.25+k*.15)/n)
                    out.append(f'<path d="M{a[0]:.2f} {a[1]:.2f}L{b[0]:.2f} {b[1]:.2f}" stroke="#8eac66" stroke-opacity=".6" stroke-width=".6"/>')
    # Paths run through panel gutters with elbow joints, like a circuit board.
    for k in range(11):
        x,y=r.randrange(2,10)/n,r.randrange(1,9)/n
        ex,ey=min(1,x+r.choice([1,2,3])/n),min(1,y+r.choice([1,2,3])/n)
        points=[point(o,u,v,x,y),point(o,u,v,x,ey),point(o,u,v,ex,ey)]
        out.append(f'<polyline points="{pstr(points)}" stroke="{r.choice(["#b5ff4d","#719e38","#91ce42"])}" stroke-width="{r.choice([1,1.5,2])}"/>')
        for a in [points[0],points[-1]]:
            out.append(f'<circle cx="{a[0]:.2f}" cy="{a[1]:.2f}" r="1.9" fill="#b5ff4d"/>')
out.append('<path d="m112 218 226 128 226-128M338 346v259" stroke="#b5ff4d" stroke-opacity=".55" stroke-width="1.4"/>')
out.append('<path d="M104 216v-13l19-11M329 83l9-5 14 8M572 215v17M338 614l-14-8" stroke="#b5ff4d" stroke-width="2"/>')
out.append('</svg>')
Path(__file__).with_name('collective-cube.svg').write_text('\n'.join(out)+'\n')
