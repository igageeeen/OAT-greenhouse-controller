/* ============================================================
   Greenhouse demo stand - printable footing parts  (rev.2)
   Printer : Bambu Lab P1S      Material : PLA
   Pipe    : greenhouse hoop pipe, OD 22.2mm

   rev.2 changes
     - pole_clamp is now a two-sided FORK: one clamp anywhere on
       the pole carries BOTH the fore and aft outrigger arms.
     - every part is shaped for its print orientation: flat base
       on the plate, no support anywhere, all downward faces >=45
       degrees, and the load pressed ACROSS layers (compression)
       rather than pulling them apart.

   Render one part at a time:  openscad -D part="foot" -o foot.stl
   ============================================================ */

part = "assembly";

// ---------------- pipe / fit ----------------
pipe_od   = 22.2;
clear     = 0.40;   // per side. vertical bores print slightly under
wall      = 3.2;    // 8 perimeters @ 0.4mm nozzle
socket_id = pipe_od + clear * 2;
socket_od = socket_id + wall * 2;

// ---------------- joint geometry ----------------
paddle_t  = 8;                    // arm paddle thickness
fork_gap  = paddle_t + 0.8;       // slip clearance inside the fork
plate_t   = 6;                    // each fork plate
plate_y   = fork_gap / 2;         // inner face of each plate

pr        = 21;   // pivot head radius (both clamp plates and arm paddle)
boss_x    = 40;   // pivot offset from the pole axis - keeps the swinging
                  // arm's paddle (radius pr) clear of the collar
boss_z    = 40;   // pivot height
pivot_d   = 8.5;  // M8 clearance
index_d   = 5.5;  // M5 clearance
index_r   = 14;   // index radius (clamp hole / arm slot)
index_a   = 40;   // clamp's fixed index hole direction, deg from +X
sweep     = 40;   // total adjustable spread, deg

// the collar must reach the full height of the plates, otherwise the top
// few mm of plate would close over the bore and the pole could not pass
collar_h  = boss_z + pr;
pin_z     = 12;   // M6 through-pin height
pin_d     = 6.5;

skirt_z   = boss_z - pr;          // where the 45deg skirt meets the head
skirt_w1  = 30;                   // skirt half width. Capped so the tube of
                                  // an arm set to the narrowest spread still
                                  // swings past it with ~5mm to spare.
over45    = 0.45;                 // extra clearance factor, unused knob

$fn = 72;

// ============================================================
// helpers
// ============================================================

// bore that opens downward on the build plate: the first layers are
// squashed outward (elephant's foot), so relieve the very bottom
module relieved_bore(d, h, relief = 0.6, relief_h = 0.8) {
    cylinder(d = d, h = h);
    translate([0, 0, -0.01]) cylinder(d = d + relief * 2, h = relief_h);
}

// 45deg lead-in at the top of a bore so the pipe starts easily.
// Widening upward at 45deg is self-supporting.
module bore_leadin(d, z, c = 2) {
    translate([0, 0, z - c]) cylinder(d1 = d, d2 = d + c * 2, h = c + 0.01);
}

// ============================================================
// PART 1 : FOOT   (prints flange-down, no support)
// The pipe passes right through and its steel end bears on the
// floor - the printed disc only widens the footprint, so PLA
// never carries the standing load.
// ============================================================
module foot(base_d = 100, base_t = 8, socket_h = 45, gusset_h = 16) {
    total_h = base_t + socket_h;
    difference() {
        union() {
            // flat, unbroken bottom: best bed adhesion and least warp
            cylinder(d = base_d, h = base_t);
            // cone narrowing upward - self-supporting, and it is the
            // gusset that carries the tipping moment into the disc
            translate([0, 0, base_t])
                cylinder(d1 = socket_od + 18, d2 = socket_od, h = gusset_h);
            translate([0, 0, base_t + gusset_h])
                cylinder(d = socket_od, h = socket_h - gusset_h);
        }
        translate([0, 0, -1]) relieved_bore(socket_id, total_h + 2);
        bore_leadin(socket_id, total_h);
        // break the sharp top rim (chamfer pointing down-inward is fine,
        // it is a tiny feature on a vertical wall)
        translate([0, 0, total_h - 1])
            difference() {
                cylinder(d = socket_od + 4, h = 1.2);
                cylinder(d1 = socket_od, d2 = socket_od - 2.4, h = 1.2);
            }
    }
}

// ============================================================
// PART 2 : POLE CLAMP  (prints collar-up, no support)
// Two parallel plates straddle the pole and reach out to BOTH
// sides, so a single clamp carries a fore and an aft arm in
// double shear. Slide it to any height on the pole, drill one
// M6 hole through the pipe and pin it.
// ============================================================

// 2D outline of one fork plate, in the XZ plane
module plate_profile() {
    difference() {
        union() {
            // stadium joining the two pivot heads
            hull() {
                translate([ boss_x, boss_z]) circle(r = pr);
                translate([-boss_x, boss_z]) circle(r = pr);
            }
            // skirt down to the build plate. Straight vertical walls, not
            // a taper: zero overhang, and it more than doubles the bed
            // contact compared with a 45deg skirt. Its width is capped by
            // what the arm needs to swing past at the narrowest setting.
            translate([-skirt_w1, 0]) square([skirt_w1 * 2, skirt_z]);
        }
        // trim the lower-outboard quarter of each head back to 45deg,
        // otherwise the underside of the circle is an unprintable
        // near-horizontal overhang. It also opens the space the arm
        // swings through.
        for (s = [1, -1]) scale([s, 1])
            polygon([[boss_x, skirt_z], [boss_x + 60, skirt_z + 60],
                     [boss_x + 60, -1], [boss_x, -1]]);
    }
}

module pole_clamp() {
    difference() {
        union() {
            cylinder(d = socket_od, h = collar_h);
            for (s = [1, -1])
                translate([0, s * (plate_y + plate_t), 0])
                    rotate([90, 0, 0])
                        linear_extrude(plate_t) plate_profile();
        }
        // pole bore
        translate([0, 0, -1]) relieved_bore(socket_id, collar_h + 2);
        bore_leadin(socket_id, collar_h);
        // M6 anti-slip pin, drilled through the pipe too. Runs along X
        // at y=0, i.e. through the open gap between the plates.
        translate([0, 0, pin_z]) rotate([0, 90, 0])
            cylinder(d = pin_d, h = 200, center = true);
        // pivot and index holes, both sides
        for (s = [1, -1]) scale([s, 1, 1]) {
            translate([boss_x, 0, boss_z]) rotate([90, 0, 0])
                cylinder(d = pivot_d, h = 200, center = true);
            translate([boss_x + index_r * cos(index_a), 0,
                       boss_z + index_r * sin(index_a)])
                rotate([90, 0, 0])
                    cylinder(d = index_d, h = 200, center = true);
        }
    }
}

// ============================================================
// PART 3 : OUTRIGGER ARM  (prints socket-up, no support)
// Local frame: pivot at the origin, pipe socket along +Z, so the
// strut load runs straight down the build direction and presses
// the layers together instead of peeling them.
// ============================================================
neck_r    = pr + 5;               // where the tube may start: clear of
                                  // the clamp's pivot head
neck_hw   = socket_od / 2;        // neck as wide as the tube: no ledge
flat_z    = -pr * cos(45);        // flat base, all overhangs >=45deg

module arc_slot(r, w, ang) {
    rotate_extrude(angle = ang, $fn = 96)
        translate([r - w / 2, 0]) square(w);
}

module arm(socket_h = 45) {
    tube_top = neck_r + socket_h;
    bore_depth = socket_h - 6;    // blind: gives the pipe a positive stop
    difference() {
        union() {
            // paddle + neck, both paddle_t thick so they live inside the fork
            rotate([90, 0, 0]) linear_extrude(paddle_t, center = true)
                difference() {
                    union() {
                        circle(r = pr);
                        translate([-neck_hw, 0]) square([neck_hw * 2, neck_r]);
                    }
                    translate([-pr - 1, flat_z - pr]) square([pr * 2 + 2, pr]);
                }
            // the socket tube, starting outside the clamp's head
            translate([0, 0, neck_r]) cylinder(d = socket_od, h = socket_h);
        }
        // pivot
        rotate([90, 0, 0]) cylinder(d = pivot_d, h = 200, center = true);
        // adjustable index slot, centred on local 180deg. The spin about the
        // slot's own axis must happen BEFORE laying it into the XZ plane -
        // rotate([90,0,c]) would apply the c about world Z and tilt it out
        // of plane.
        rotate([90, 0, 0]) rotate([0, 0, 180 - sweep / 2])
            arc_slot(index_r, index_d, sweep);
        // pipe socket - a vertical bore prints round and needs no support
        translate([0, 0, tube_top - bore_depth])
            cylinder(d = socket_id, h = bore_depth + 1);
        bore_leadin(socket_id, tube_top);
    }
}

// ============================================================
if (part == "foot") foot();
else if (part == "pole_clamp") pole_clamp();
else if (part == "arm") arm();
else if (part == "assembly") {
    theta = [25, 55];   // two arms, deliberately at different spreads
    pole_clamp();
    for (i = [0, 1]) scale([i == 0 ? 1 : -1, 1, 1])
        color("orange")
            translate([boss_x, 0, boss_z]) rotate([0, 180 - theta[i], 0]) arm();
    // the pole itself, for context
    %translate([0, 0, -40]) cylinder(d = pipe_od, h = 160);
}
