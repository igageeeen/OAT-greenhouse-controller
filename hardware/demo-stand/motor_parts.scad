/* ============================================================
   Roll-up drive fixtures for the maker-faire demo   (rev.1)
   Printer : Bambu Lab P1S      Material : PLA

   Drive train
     5840-31ZY worm gear motor, DC12V 30RPM, 8mm D output shaft
     (output shaft is at RIGHT ANGLES to the motor body)
       -> coupler -> 19mm winding pipe, carrying a 50x60cm sheet
     Support posts are 22mm pipe.

   All three parts print standing on the collar axis: flat base on
   the plate, no support, and every bore that matters is vertical.

   Render:  openscad -D part="motor_plate" -o motor_plate.stl motor_parts.scad
     "motor_plate"    - clamps a 22mm post, motor bolts/straps to it
     "bearing_block"  - clamps a 22mm post, journals the 19mm pipe
     "coupler"        - 8mm D shaft -> 19mm pipe
     "assembly"       - layout preview
   ============================================================ */

part = "assembly";

// ---- pipes -------------------------------------------------
post_od   = 22.2;   // support post
roll_od   = 19.1;   // winding pipe
wall      = 3.2;

// ---- motor (5840-31ZY) ------------------------------------
// VERIFY THESE THREE WITH CALIPERS BEFORE PRINTING THE COUPLER:
shaft_d    = 8.0;   // output shaft diameter
shaft_flat = 7.3;   // across the D flat
shaft_len  = 14;    // how far the shaft stands off the gearbox face

// ---- layout ------------------------------------------------
// the winding pipe runs alongside the post, offset by axis_off.
// 48mm keeps the outer end of the bolt slots clear of the collar and
// leaves the 40mm-wide gearbox well clear of the post.
axis_off  = 48;
collar_h  = 40;
pin_d     = 5.5;    // M5 pin, drilled through the post
plate_t   = 6;
plate_w   = 70;     // motor plate is square-ish, plate_w x plate_w

post_id   = post_od + 0.8;
post_ring = post_id + wall * 2;
roll_run  = roll_od + 0.9;          // running clearance in the journal
roll_grip = roll_od + 0.5;          // grip fit in the coupler
jour_od   = roll_run + wall * 2;

axis_z    = collar_h - 6;           // shaft / journal height on the collar

$fn = 72;

// ============================================================
// shared: collar that slides onto the 22mm post and is pinned
// through a hole drilled in the pipe
// ============================================================
module post_collar(h = collar_h) {
    difference() {
        cylinder(d = post_ring, h = h);
        translate([0, 0, -1]) cylinder(d = post_id, h = h + 2);
        // lead-in so the post starts easily (45deg, self-supporting)
        translate([0, 0, h - 2]) cylinder(d1 = post_id, d2 = post_id + 4, h = 2.01);
        // M5 cross pin, low down and clear of everything above
        translate([0, 0, 10]) rotate([0, 90, 0])
            cylinder(d = pin_d, h = 200, center = true);
    }
}

// ============================================================
// PART 1 : BEARING BLOCK
// A web wall rises from the plate and the journal sits on top of
// it, so the underside of the journal bore is printed solid from
// below - which is exactly the surface the pipe rests on.
// ============================================================
module bearing_block(jour_len = 34, web_t = 14) {
    difference() {
        union() {
            post_collar();
            // supporting web: a slab standing on the bed
            translate([-web_t / 2, -post_ring / 2, 0])
                cube([web_t, axis_off + jour_od / 2 + post_ring / 2, axis_z]);
            translate([0, axis_off, axis_z]) rotate([0, 90, 0])
                cylinder(d = jour_od, h = jour_len, center = true);
        }
        // bore right through the whole part, not just the collar, so the
        // post may run on past it - these can sit anywhere on the pipe
        translate([0, 0, -1]) cylinder(d = post_id, h = 200);
        translate([0, 0, 10]) rotate([0, 90, 0])
            cylinder(d = pin_d, h = 200, center = true);
        // the journal itself, through-bored
        translate([0, axis_off, axis_z]) rotate([0, 90, 0])
            cylinder(d = roll_run, h = 200, center = true);
    }
}

// ============================================================
// PART 2 : MOTOR PLATE
// The gearbox face bolts flat against this plate. The 5840-31ZY
// has four M4 holes but the pitch is not published, so the plate
// carries eight radial SLOTS instead - any square or rectangular
// pattern with a half-diagonal between 15 and 28mm lands on them.
// If the real pattern misses, two strap slots let you simply
// zip-tie the gearbox down instead.
// ============================================================
module motor_plate() {
    difference() {
        union() {
            post_collar();
            // the plate: a vertical slab, normal along the shaft axis.
            // It runs all the way back past the collar so the two are
            // properly fused - the motor hangs off this joint.
            translate([-plate_t / 2, -post_ring / 2, 0])
                cube([plate_t, axis_off + plate_w / 2 + post_ring / 2,
                      axis_z + plate_w / 2]);
        }
        // bore right through the whole part, not just the collar, so the
        // post may run on past it - these can sit anywhere on the pipe
        translate([0, 0, -1]) cylinder(d = post_id, h = 200);
        translate([0, 0, 10]) rotate([0, 90, 0])
            cylinder(d = pin_d, h = 200, center = true);
        translate([0, axis_off, axis_z]) rotate([0, 90, 0]) {
            // clearance for the shaft and its boss - pure clearance, so
            // the droop of a horizontal bore does not matter here
            cylinder(d = 22, h = 200, center = true);
            // eight radial M4 slots
            for (a = [0 : 45 : 359]) rotate([0, 0, a])
                hull() for (r = [15, 28])
                    translate([r, 0, 0]) cylinder(d = 4.5, h = 200, center = true);
            // strap slots for a zip tie round the gearbox
            for (s = [-1, 1]) translate([0, s * 30, 0])
                hull() for (x = [-6, 6])
                    translate([x, 0, 0]) cylinder(d = 4.5, h = 200, center = true);
        }
    }
}

// ============================================================
// PART 3 : COUPLER   8mm D shaft -> 19mm pipe
// The D flat carries the torque; the M4 cross bolt through the
// pipe stops it slipping, and a second cross screw stops the
// coupler walking off the shaft.
// ============================================================
module coupler(grip_len = 30, web = 7) {
    d_depth = shaft_len - 2;
    total   = d_depth + web + grip_len;
    od      = roll_grip + wall * 2;
    difference() {
        cylinder(d = od, h = total);
        // D bore for the motor shaft (at the bottom in print orientation).
        // The flat sits shaft_flat away from the far side of the circle,
        // i.e. (shaft_flat - shaft_d/2) below centre, plus a little slack.
        translate([0, 0, -1]) intersection() {
            cylinder(d = shaft_d + 0.3, h = d_depth + 1);
            translate([-10, -(shaft_flat - shaft_d / 2) - 0.15, 0])
                cube([20, 20, d_depth + 1]);
        }
        // socket for the winding pipe
        translate([0, 0, d_depth + web]) cylinder(d = roll_grip, h = grip_len + 1);
        // M4 through the pipe - drill the pipe to match
        translate([0, 0, total - 14]) rotate([90, 0, 0])
            cylinder(d = 4.3, h = 200, center = true);
        // M4 set screw onto the D flat
        translate([0, 0, d_depth / 2]) rotate([90, 0, 0])
            cylinder(d = 3.3, h = 200, center = true);
    }
}

// ============================================================
if (part == "bearing_block") bearing_block();
else if (part == "motor_plate") motor_plate();
else if (part == "coupler") coupler();
else if (part == "assembly") {
    motor_plate();
    translate([0, 0, 0]) color("orange")
        translate([-40, axis_off, axis_z]) rotate([0, 90, 0]) coupler();
    translate([260, 0, 0]) bearing_block();
    // winding pipe
    color("silver") translate([-10, axis_off, axis_z]) rotate([0, 90, 0])
        cylinder(d = roll_od, h = 300);
}
