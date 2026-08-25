/* ============================================================
   Greenhouse demo stand - printable footing parts
   Printer : Bambu Lab P1S      Material : PLA
   Pipe    : greenhouse hoop pipe, OD 22.2mm (measured)

   Parts (render one at a time with -D part="..."):
     "foot"        - goes on every leg's ground-contact end
     "pole_clamp"  - clamps to the main upright pole, carries
                     the pivot for an outrigger leg
     "arm"         - socket for the outrigger pipe, bolts to
                     pole_clamp with an adjustable-angle slot
     "assembly"    - preview of pole_clamp + arm mated together
   ============================================================ */

part = "assembly"; // overridden from the command line with -D part="..."

// ---------------- shared parameters ----------------
pipe_od   = 22.2;   // measured pipe OD - change if your pipe differs
clear     = 0.35;   // per-side clearance for a snug slip/press fit
wall      = 3.2;    // socket wall thickness (~8 perimeters @0.4mm nozzle)

socket_id = pipe_od + clear * 2;
socket_od = socket_id + wall * 2;

$fn = 72;

// ============================================================
// PART 1 : FOOT
// Pipe passes fully through; the steel tip bears on the floor,
// the printed disc only widens the footprint against tipping.
// ============================================================
module foot(base_d = 100, base_t = 8, socket_h = 45, gusset_h = 14, chamfer = 1.5) {
    // stacked exactly (no epsilon gaps) so every segment's end matches the next one's start
    wall_h = socket_h - gusset_h - chamfer;
    total_h = base_t + socket_h;
    difference() {
        union() {
            cylinder(d = base_d, h = base_t);
            translate([0, 0, base_t])
                cylinder(d1 = socket_od + 16, d2 = socket_od, h = gusset_h);
            translate([0, 0, base_t + gusset_h])
                cylinder(d = socket_od, h = wall_h);
            translate([0, 0, base_t + gusset_h + wall_h])
                cylinder(d1 = socket_od, d2 = socket_od + chamfer * 2, h = chamfer);
        }
        translate([0, 0, -1])
            cylinder(d = socket_id, h = total_h + 2);
        // shallow recess on the underside for a self-adhesive rubber/felt pad
        translate([0, 0, -0.01])
            cylinder(d = base_d - 20, h = 1.6);
    }
}

// ============================================================
// PART 2 : POLE CLAMP
// Slides over the main upright pole and is pinned through a
// hole drilled in the pipe (M6 bolt) so it can't rotate or
// slip under load. Carries the fixed pivot boss.
// ============================================================
boss_d      = 52;   // pivot boss disc diameter
boss_t      = 8;    // pivot boss disc thickness
pivot_d     = 8.5;  // M8 clearance
index_d     = 5.5;  // M5 clearance (anti-rotation pin)
index_r     = 18;   // radius of the anti-rotation pin from the pivot centre
slot_angle  = 45;   // total adjustable sweep, degrees
collar_h    = 45;   // pole_clamp collar height
boss_z      = 33;   // boss centre height on the collar (kept clear of the pin hole below)
pin_z       = 12;   // M6 anti-slip pin height on the collar
boss_x      = socket_od / 2 + 4; // pivot boss offset from the pole axis

module boss_blank(d, t) {
    // a disc whose face-normal is horizontal and tangential to the pole,
    // i.e. the disc itself lies in the vertical plane containing the pole
    // axis and the radial (outward) direction - so spinning it about its
    // own normal tilts the arm from "near the pole" to "spread wide".
    rotate([90, 0, 0]) cylinder(d = d, h = t, center = true);
}

module pole_clamp(pin_d = 6.5) {
    // boss centre is set so the disc overlaps deep past the collar's own
    // axis - a plain union (no hull) then fuses everything solidly, since
    // the two shapes genuinely intersect rather than merely touch.
    difference() {
        union() {
            cylinder(d = socket_od, h = collar_h);
            translate([boss_x, 0, boss_z])
                boss_blank(boss_d, boss_t);
        }
        // through-bore for the pole
        translate([0, 0, -1])
            cylinder(d = socket_id, h = collar_h + 2);
        // cross-bore for the M6 pin (also passes through the pipe wall) -
        // kept well clear of the boss so it doesn't eat into that material
        translate([0, 0, pin_z])
            rotate([90, 0, 0])
                cylinder(d = pin_d, h = socket_od + 4, center = true);
        // single fixed anti-rotation pin hole, straight "up" (+Z) from the pivot centre
        translate([boss_x, 0, boss_z + index_r])
            rotate([90, 0, 0]) cylinder(d = index_d, h = boss_t + 2, center = true);
        // pivot bolt clearance through the boss centre
        translate([boss_x, 0, boss_z])
            rotate([90, 0, 0]) cylinder(d = pivot_d, h = boss_t + 2, center = true);
        // recessed hex pocket on the outer (+Y) face so the M8 bolt head doesn't spin;
        // the arm mates against the -Y face
        translate([boss_x, boss_t / 2, boss_z])
            rotate([90, 0, 0])
                cylinder(d = 15, h = 5, $fn = 6);
    }
}

// ============================================================
// PART 3 : OUTRIGGER ARM
// Socket for the outrigger pipe; bolts to pole_clamp's boss.
// An arc SLOT (not fixed holes) gives continuously adjustable
// spread angle - loosen, swing to the width you want, retighten.
// ============================================================
module arc_slot(r, w, ang) {
    // an arc-shaped slot of width w, sweeping +/-ang/2 around Z, at radius r
    rotate_extrude(angle = ang, $fn = 96)
        translate([r - w / 2, 0])
            square(w);
}

module arm(socket_h = 45, chamfer = 1.5) {
    // boss disc centred on the pivot (local origin); the outrigger socket tube
    // starts deep inside the disc (strong overlap, plain union - no hull) and
    // protrudes outward along local +X.
    tube_x0 = 8;         // where the tube's outer shape starts, embedded in the boss
    tube_len = tube_x0 + socket_h;
    bore_depth = socket_h - 8; // stop short of the boss core - gives the pipe a positive
                               // stop and keeps the pivot-bolt area solid
    difference() {
        union() {
            boss_blank(boss_d, boss_t);
            translate([tube_x0, 0, 0])
                rotate([0, 90, 0]) {
                    cylinder(d = socket_od, h = tube_len - tube_x0 - chamfer);
                    translate([0, 0, tube_len - tube_x0 - chamfer])
                        cylinder(d1 = socket_od, d2 = socket_od + chamfer * 2, h = chamfer);
                }
        }
        // pivot bolt clearance through the boss centre
        rotate([90, 0, 0]) cylinder(d = pivot_d, h = boss_t + 2, center = true);
        // arc slot: same radius as pole_clamp's fixed index hole, swept over the
        // adjustable range - loosen the M5 bolt, slide to the width you want, retighten
        rotate([90, 0, -90 + slot_angle / 2])
            arc_slot(index_r, index_d, slot_angle);
        // blind bore for the outrigger pipe - opens at the outer tip, stops short
        // of the boss core so the pipe has a positive stop (not a through hole)
        translate([tube_len - bore_depth, 0, 0])
            rotate([0, 90, 0])
                cylinder(d = socket_id, h = bore_depth + 1);
    }
}

// ============================================================
// render selection
// ============================================================
if (part == "foot") foot();
else if (part == "pole_clamp") pole_clamp();
else if (part == "arm") arm();
else if (part == "assembly") {
    preview_angle = 30; // pick any angle inside the slot's sweep, just for the preview
    pole_clamp();
    color("orange")
        translate([boss_x, -boss_t, boss_z])
            rotate([0, preview_angle, 0])
                arm();
}
