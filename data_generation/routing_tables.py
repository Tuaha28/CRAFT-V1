"""
Literature-derived per-garment-type routing tables -- replaces the invented
uniform 5-step template (`for step in range(1, 6): m_type = machine_types
[step-1]`) that made Tubular Sleeve Join a universal, mandatory LAST step
for every order regardless of garment type. See routing_spec.md for the
full derivation, sources, and the decisive finding: no published operation
breakdown shares a common final operation across garment types, and
Feed-off-the-Arm Special (Tubular Sleeve Join) is a MID-SEQUENCE operation
in every one of them -- it was put at position 5-of-5 for every product by
the old template, not because real routings put it there.

Each routing is a list of (operation_name, machine_type) pairs, machine_type
being one of the five canonical strings the rest of this codebase already
keys on (utils/machine_taxonomy.py::MACHINE_TYPE_TO_OPERATION_TYPE). Routing
length varies 6-10 steps by group (the spec's own examples run 7-10; the
Tank Top variant below is 6, the shortest, after omitting two sleeve steps
it has no use for; the Blouse/Dress short-sleeve variant is 8, one less
than its 9-step base). Nothing downstream depends on a fixed step count --
data_generation/seed_database_fixed.py's own routing-consumption loop
already iterates `enumerate(operations_chain)` generically, and every
consumer of Routing_Operations in the live pipeline (ga/, graph/, agent/,
models/) reads operation_sequence_number and iterates the routing table
directly rather than assuming a length -- verified before writing this.

HONEST LIMITATION (from routing_spec.md itself, carried forward here):
these are published industry-standard breakdowns, not Needle Craft's own
process sheets (unavailable). Representative of garment manufacturing
practice and cross-validated against the company's own real GarmentSmv
figures (see SMV_BY_GARMENT_TYPE below), but the exact sequences at this
specific factory remain unconfirmed.
"""

# ---------------------------------------------------------------------------
# The five canonical machine-type strings this codebase already keys on
# (must match utils/machine_taxonomy.py::MACHINE_TYPE_TO_OPERATION_TYPE's
# keys, normalised). Not redefined here as a separate vocabulary -- these
# are the same five types the old template used, just no longer applied in
# one fixed order/count.
# ---------------------------------------------------------------------------
SNLS = "Single-Needle Lockstitch"
OVERLOCK = "Overlock Serger (4-Thread)"
FLATLOCK = "Flatlock Coverstitch"
TACKER = "Automated Pattern Tacker"
FOA = "Feed-off-the-Arm Special"


# ---------------------------------------------------------------------------
# Routing tables by group (routing_spec.md Section 3). Terminal operation
# (the last tuple's machine type) is the whole point of this exercise --
# see routing_spec.md Section 4's terminal-operation distribution table.
# ---------------------------------------------------------------------------

# 3.1 Knit tops, base sequence (U Neck, V Neck, Mock Neck use this as-is).
# Terminal: Flatlock Coverstitch (bottom hem).
KNIT_TOP = [
    ("Shoulder join", OVERLOCK),
    ("Neck rib attach", OVERLOCK),
    ("Neck topstitch", SNLS),
    ("Sleeve attach", OVERLOCK),
    ("Side seam + underarm close", FOA),
    ("Sleeve hem", FLATLOCK),
    ("Bottom hem", FLATLOCK),
]

# Tank Top: knit-top base with steps 4 (sleeve attach) and 6 (sleeve hem)
# omitted (no sleeves) and an armhole-binding step added in their place,
# per routing_spec.md 3.1's variant note. Terminal: Flatlock Coverstitch.
TANK_TOP = [
    ("Shoulder join", OVERLOCK),
    ("Neck rib attach", OVERLOCK),
    ("Neck topstitch", SNLS),
    ("Armhole binding", OVERLOCK),
    ("Side seam + underarm close", FOA),
    ("Bottom hem", FLATLOCK),
]

# Polo: knit-top base plus placket topstitch and buttonhole/button attach
# (both Single-Needle Lockstitch), per routing_spec.md 3.1's variant note.
# Inserted BEFORE the sleeve/hem steps (not appended after) so the
# terminal operation stays Flatlock Coverstitch, matching every other
# knit-top routing and routing_spec.md Section 4's terminal table -- the
# whole point being a consistent, non-arbitrary terminal per group.
POLO = [
    ("Shoulder join", OVERLOCK),
    ("Collar attach", OVERLOCK),
    ("Placket attach", SNLS),
    ("Placket topstitch", SNLS),
    ("Buttonhole + button attach", SNLS),
    ("Sleeve attach", OVERLOCK),
    ("Side seam + underarm close", FOA),
    ("Sleeve hem", FLATLOCK),
    ("Bottom hem", FLATLOCK),
]

# 3.2 Trousers / shorts / leggings (Pant, Short, Skirt).
# Terminal: Automated Pattern Tacker (bartack reinforcement).
TROUSER = [
    ("Panel serging", OVERLOCK),
    ("Pocket attach / welt", SNLS),
    ("Fly / zip attach", SNLS),
    ("Inseam close", FOA),
    ("Side seam close", OVERLOCK),
    ("Waistband attach", SNLS),
    ("Belt loop attach", TACKER),
    ("Bottom hem", FLATLOCK),
    ("Bartack reinforcement", TACKER),
]

# 3.3 Wovens (Shirt, Blouse, Dress). Terminal: Automated Pattern Tacker
# (buttonhole + button attach).
WOVEN = [
    ("Panel serging", OVERLOCK),
    ("Placket attach", SNLS),
    ("Yoke / shoulder join", SNLS),
    ("Collar make + attach", SNLS),
    ("Sleeve attach", OVERLOCK),
    ("Side seam + armhole close", FOA),
    ("Cuff attach", SNLS),
    ("Bottom hem", SNLS),
    ("Buttonhole + button attach", TACKER),
]

# routing_spec.md 3.3's short-sleeve variant for Blouse/Dress: omit step 7
# (Cuff attach) -- a short sleeve has no cuff to close. 8 steps, same
# terminal operation. Re-verified before implementing that no real field
# (grid.xls Group/Layout/StyleId, Plan.xlsx Season/GroupNo/MasterGroup) can
# key which real rows are short- vs long-sleeve -- Season is 100% null,
# Group/GroupNo/StyleId are batch/production codes with no sleeve-length
# pattern in 20 sampled Blouse/Dress values. No source exists (bucket c);
# which specific real garment is short-sleeved cannot be derived, only
# whether the STRUCTURE exists in the routing at all. Applied as a
# per-product random choice at SHORT_SLEEVE_SHARE (seed_database_fixed.py),
# not folded into the table dict below since it isn't keyed by GarmentType
# alone -- see routing_for()'s short_sleeve parameter.
WOVEN_SHORT_SLEEVE = [
    ("Panel serging", OVERLOCK),
    ("Placket attach", SNLS),
    ("Yoke / shoulder join", SNLS),
    ("Collar make + attach", SNLS),
    ("Sleeve attach", OVERLOCK),
    ("Side seam + armhole close", FOA),
    ("Bottom hem", SNLS),
    ("Buttonhole + button attach", TACKER),
]

# 3.4 Zipped outerwear (Hoodie, Full Zip Hoodie, Full/Half Zip Mock Neck,
# 1/2 Zipper Hoodie). Terminal: Automated Pattern Tacker (bartack).
ZIPPED_OUTERWEAR = [
    ("Shoulder join", OVERLOCK),
    ("Hood / mock-neck collar make", OVERLOCK),
    ("Zip attach", SNLS),
    ("Hood / collar attach", OVERLOCK),
    ("Pocket attach", SNLS),
    ("Sleeve attach", OVERLOCK),
    ("Side seam + underarm close", FOA),
    ("Cuff / sleeve rib attach", FLATLOCK),
    ("Bottom rib / hem", FLATLOCK),
    ("Bartack (pocket corners, zip base)", TACKER),
]


# ---------------------------------------------------------------------------
# GarmentType (grid.xls real values) -> routing table.
#
# Direct spec mappings (routing_spec.md Section 3) plus a documented
# fallback for the three tiny-count real categories the spec doesn't cover
# (Jump Suit 5 rows, Tube 3 rows, Helmet 2 rows of 5,132 -- 0.2% combined):
# Jump Suit -> TROUSER (a one-piece garment closer to trouser assembly
# complexity than a knit top), Tube -> TANK_TOP (a tube top is a sleeveless,
# collarless knit -- the closest existing table), Helmet -> WOVEN (not a
# garment in the conventional sense; too rare to warrant its own researched
# routing, defaulted to the generic multi-step woven table rather than the
# shortest/simplest one so it doesn't silently look trivial).
# ---------------------------------------------------------------------------
GARMENT_TYPE_TO_ROUTING = {
    "U Neck": KNIT_TOP,
    "V Neck": KNIT_TOP,
    "Mock Neck": KNIT_TOP,
    "Tank Top": TANK_TOP,
    "Polo": POLO,
    "Pant": TROUSER,
    "Short": TROUSER,
    "Skirt": TROUSER,
    "Shirt": WOVEN,
    "Blouse": WOVEN,
    "Dress": WOVEN,
    "Hoodie": ZIPPED_OUTERWEAR,
    "Full Zip Hoodie": ZIPPED_OUTERWEAR,
    "Full Zip Mock Neck": ZIPPED_OUTERWEAR,
    "1/2 Zip Mock Neck": ZIPPED_OUTERWEAR,
    "1/2 Zipper Hoodie": ZIPPED_OUTERWEAR,
    # Fallbacks for tiny-count real categories not covered by the spec:
    "Jump Suit": TROUSER,
    "Tube": TANK_TOP,
    "Helmet": WOVEN,
}

# Group name each table belongs to, for the fallback-SMV lookup below (a
# GarmentType with no real Plan.xlsx SMV entry falls back to its group's
# average across the types that DO have one, not a single global number).
_ROUTING_GROUP_NAME = {
    id(KNIT_TOP): "knit_top", id(TANK_TOP): "knit_top", id(POLO): "knit_top",
    id(TROUSER): "trouser",
    id(WOVEN): "woven", id(WOVEN_SHORT_SLEEVE): "woven",
    id(ZIPPED_OUTERWEAR): "zipped_outerwear",
}

# DELIBERATE SYNTHETIC CHOICE (bucket c), documented rather than left an
# unexplained magic number: with no real field able to key which actual
# Blouse/Dress garments are short-sleeved (see WOVEN_SHORT_SLEEVE's
# comment), an even 50/50 split is the simplest defensible assumption
# absent any signal either way. Applied per-product in
# seed_database_fixed.py's product loop, and blended at this same ratio
# into the aggregate machine-demand and SMV-calibration sums computed
# before individual products exist.
SHORT_SLEEVE_SHARE = 0.5


def routing_group_name(garment_type: str) -> str:
    """Which of the four routing families a real GarmentType maps to."""
    table = GARMENT_TYPE_TO_ROUTING.get(garment_type, WOVEN)
    return _ROUTING_GROUP_NAME.get(id(table), "woven")


def routing_for(garment_type: str, short_sleeve: bool = False) -> list:
    """
    The (operation_name, machine_type) list for a real GarmentType string.
    Unknown/unmapped types (shouldn't happen for anything actually drawn
    from grid.xls's real distribution, but a synthetic/test caller could
    pass anything) fall back to WOVEN -- the most "generic multi-step"
    table, matching the Helmet fallback's own reasoning above.

    short_sleeve: for Blouse/Dress only, selects the 8-step cuff-omission
    variant (routing_spec.md 3.3) instead of the 9-step base. Ignored for
    every other garment type -- no other group has a documented variant.
    """
    if short_sleeve and garment_type in ("Blouse", "Dress"):
        return WOVEN_SHORT_SLEEVE
    return GARMENT_TYPE_TO_ROUTING.get(garment_type, WOVEN)


def sam_per_step(total_garment_smv: float, num_steps: int) -> float:
    """
    Distributes a garment's total real SMV equally across its routing
    steps -- routing_spec.md Section 5 calls for a distribution
    "proportional to typical operation counts per machine class in the
    sources". Equal-per-STEP division already implements exactly that: a
    machine class used in 3 of a routing's 7 steps automatically receives
    3/7 of the total, proportional to its own step count, with no extra
    per-operation weighting invented beyond what the routing table's own
    step composition already encodes.
    """
    if num_steps <= 0:
        return 0.0
    return total_garment_smv / num_steps
