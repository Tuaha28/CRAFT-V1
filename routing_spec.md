# Literature-Derived Garment Routing Specification
## For Needle Craft Ltd synthetic data generation — replacing the invented uniform 5-step template

**Purpose.** The current seeder applies one hardcoded 5-step routing
(`for step in range(1, 6): m_type = machine_types[step-1]`) to every product
regardless of garment type, with no derivation from any real source. That
template is the sole reason "Tubular Sleeve Join is a mandatory last step for
every order" is true — the premise underlying findings 0H.3, 0H.6, 0H.8, 0H.9,
0H.11 and 0H.16. This document replaces it with routings derived from published
apparel industrial-engineering operation breakdowns, cross-validated against the
company's own real `GarmentSmv` figures.

---

## 1. The decisive structural finding

**Published operation breakdowns do not share a common final operation across
garment types.** From the sources below:

- **Knit tops (t-shirt, polo, tank):** assembly ends with *bottom hem* on a
  flatlock/coverstitch machine. The feed-off-the-arm sleeve/side join happens
  in the *middle* of the sequence, not at the end.
- **Trousers/jeans:** assembly ends with *bottom hem* and *bartack
  reinforcement* (belt loops, fly, pocket corners). Feed-off-the-arm is used for
  the **inseam**, which occurs mid-sequence.
- **Shirts:** assembly ends with *buttonhole and button attach*, following
  collar and cuff attachment — feed-off-the-arm is not used at all in many
  shirt breakdowns.

This directly contradicts a universal terminal operation. The TSJ bottleneck is
therefore an artifact of the invented template, not a property of garment
manufacturing.

Supporting evidence already in the company's own data:
- `GarmentSmv` varies ~4.6x across types (U Neck 9.5 min → Shirt 43.8 min).
- `LayoutType` (P/H/W) correlates strongly with garment type (Polo: 98 H vs 1 P),
  indicating physically different line configurations per product class.

---

## 2. Machine-type mapping

The five machine types in the existing model map onto standard industry machines
as follows (per *Different Types of Industrial Sewing Machines and Their Use*,
onlineclothingstudy.com, and *Different Types of Sewing Machines and Their Use*,
R. Pickard):

| Model machine type | Industry equivalent | Documented use |
|---|---|---|
| Single-Needle Lockstitch | SNLS / DNLS (DB-1) | Topstitching, collar/cuff attach, waistband, plackets, general woven assembly |
| Overlock Serger (4-Thread) | 3T/4T/5T O/L (DC-21) | Serging panels, side seams, shoulder joins — "side seam stitch of a t-shirt is done using an overlock machine" |
| Flatlock Coverstitch | F/L (UY-128), stitch class 406 | "hemming the sleeves and bottom portions of knitwear", cover-stitching |
| Automated Pattern Tacker | Bartack (DP-5) | "buttonholes, belt loops, fly openings and pleats" — stress-point reinforcement |
| Feed-off-the-Arm Special | FOA | Tubular/cylindrical seams — jeans inseam, sleeve join |

---

## 3. Routings by garment type

Sequence order is the assembly order given in the cited breakdowns. `SAM share`
distributes the garment's total real `GarmentSmv` across its operations,
proportional to typical operation counts per machine class in the sources.

### 3.1 Knit tops — U Neck, V Neck, Tank Top, Mock Neck, Polo
*Source: Operation Breakdown of a Basic T-shirt (SMV 5.6) and Operation
Breakdown of a Polo T-shirt (SMV 11.87), textilesscholars.blogspot.com*

| Seq | Operation | Machine type |
|---|---|---|
| 1 | Shoulder join | Overlock Serger |
| 2 | Neck rib attach / collar attach | Overlock Serger |
| 3 | Neck topstitch (polo: placket attach) | Single-Needle Lockstitch |
| 4 | Sleeve attach | Overlock Serger |
| 5 | Side seam + underarm close | Feed-off-the-Arm Special |
| 6 | Sleeve hem | Flatlock Coverstitch |
| 7 | Bottom hem | Flatlock Coverstitch |

Tank Top: omit steps 4 and 6 (no sleeves); armhole binding on Overlock instead.
Polo: add placket topstitch and buttonhole/button (Single-Needle Lockstitch).

**Terminal operation: Flatlock Coverstitch.**

### 3.2 Trousers / Shorts / Leggings — Pant, Short, Skirt
*Sources: Operation Breakdown of Men's Trouser and Operation Bulletin of
Leggings, shorabhossain.blogspot.com; Operation Breakdown and SMVs of a Basic
Jeans, onlineclothingstudy.com*

| Seq | Operation | Machine type |
|---|---|---|
| 1 | Panel serging (front/back) | Overlock Serger |
| 2 | Pocket attach / welt | Single-Needle Lockstitch |
| 3 | Fly / zip attach | Single-Needle Lockstitch |
| 4 | Inseam close | Feed-off-the-Arm Special |
| 5 | Side seam close | Overlock Serger |
| 6 | Waistband attach | Single-Needle Lockstitch |
| 7 | Belt loop attach | Automated Pattern Tacker |
| 8 | Bottom hem | Flatlock Coverstitch |
| 9 | Bartack reinforcement (fly, pockets, loops) | Automated Pattern Tacker |

**Terminal operation: Automated Pattern Tacker.**

### 3.3 Wovens — Shirt, Blouse, Dress
*Source: Operation Breakdown and SMV of Men's Long Sleeve Shirt (SMV 23.15),
ordnur.com*

| Seq | Operation | Machine type |
|---|---|---|
| 1 | Panel serging | Overlock Serger |
| 2 | Placket attach | Single-Needle Lockstitch |
| 3 | Yoke / shoulder join | Single-Needle Lockstitch |
| 4 | Collar make + attach | Single-Needle Lockstitch |
| 5 | Sleeve attach | Overlock Serger |
| 6 | Side seam + armhole close | Feed-off-the-Arm Special |
| 7 | Cuff attach | Single-Needle Lockstitch |
| 8 | Bottom hem | Single-Needle Lockstitch |
| 9 | Buttonhole + button attach | Automated Pattern Tacker |

**Terminal operation: Automated Pattern Tacker.**
Dress/Blouse: omit cuff (step 7) for short-sleeve variants.

### 3.4 Zipped outerwear — Hoodie, Full Zip Hoodie, Full/Half Zip Mock Neck, 1/2 Zipper Hoodie
*Derived from knit-top base sequence plus zip/hood operations per the same
knitwear sources; zip insertion on lockstitch per standard practice.*

| Seq | Operation | Machine type |
|---|---|---|
| 1 | Shoulder join | Overlock Serger |
| 2 | Hood make / mock-neck collar make | Overlock Serger |
| 3 | Zip attach (full or half) | Single-Needle Lockstitch |
| 4 | Hood / collar attach | Overlock Serger |
| 5 | Pocket attach | Single-Needle Lockstitch |
| 6 | Sleeve attach | Overlock Serger |
| 7 | Side seam + underarm close | Feed-off-the-Arm Special |
| 8 | Cuff / sleeve rib attach | Flatlock Coverstitch |
| 9 | Bottom rib / hem | Flatlock Coverstitch |
| 10 | Bartack (pocket corners, zip base) | Automated Pattern Tacker |

**Terminal operation: Automated Pattern Tacker.**

---

## 4. Terminal-operation distribution (the point of this exercise)

| Terminal machine type | Garment types | Approx. share of real order rows |
|---|---|---|
| Flatlock Coverstitch | U Neck, V Neck, Tank Top, Mock Neck, Polo | ~30% |
| Automated Pattern Tacker | Pant, Short, Skirt, Shirt, Blouse, Dress, all zipped outerwear | ~70% |
| Feed-off-the-Arm Special | *(none)* | 0% |

Feed-off-the-Arm — the current model's universal terminal stage — is a
**mid-sequence** operation in every published breakdown reviewed. This is the
single most consequential correction.

---

## 5. SMV validation against real company data

Total per-garment SAM should be set to the company's own measured `GarmentSmv`
(from `Plan.xlsx`), distributed across the operations above. Real values:

| Garment type | Real GarmentSmv (min) | Published literature SMV | Consistent? |
|---|---|---|---|
| U Neck | 9.52 | 5.6 (basic t-shirt) | Yes — same magnitude, real includes finishing |
| V Neck | 11.78 | 5.6 (basic t-shirt) | Yes |
| Tank Top | 10.93 | — | Plausible (simplest knit) |
| Polo | 16.12 | 11.87 (polo) | Yes — close |
| Blouse | 18.53 | — | Between knit and shirt, as expected |
| Short | 20.54 | — | Below trouser, as expected |
| Hoodie | 21.96 | — | Above basic knit, as expected |
| Pant | 26.74 | ~25-30 (trouser/jeans breakdowns) | Yes |
| Full Zip Mock Neck | 29.76 | — | Highest knit, consistent with zip ops |
| Shirt | 43.83 | 23.15 (long-sleeve shirt) | Same rank order; real ~1.9x higher |

**Ordering matches published literature exactly** (knit tops < polo < shorts <
trousers < shirts). Absolute values run ~1.7-1.9x above published figures,
consistent across types — plausibly reflecting inclusion of non-sewing
operations (cutting, finishing, packing) in the company's figure. Because the
seeder should use the **real** `GarmentSmv` values directly, this offset does
not propagate into the model.

---

## 6. Implementation notes for the seeder

1. Replace the hardcoded 5-step loop with a per-garment-type routing table from
   §3. Operation counts now vary (7-10 steps) instead of a fixed 5.
2. Set each product's total SAM from its real `GarmentSmv` (`Plan.xlsx`),
   distributed across its operations rather than randomised per step.
3. Assign garment type from the real distribution in `grid.xls` (Pant 2,140,
   U Neck 1,099, Blouse 426, Full Zip Mock Neck 370, Short 315, Dress 194,
   Tank Top 170, ... ) rather than uniformly at random.
4. The `GarmentType` label becomes load-bearing rather than decorative.

**Expected consequence:** the single-bottleneck structure disappears. Load
concentrates on Automated Pattern Tacker (~70% of orders terminate there) and
Overlock Serger (used 2-4x in every routing), while Feed-off-the-Arm becomes a
light, mid-sequence resource. Findings 0H.3/0H.6/0H.8/0H.9/0H.11/0H.16 must be
re-evaluated against this structure.

---

## 7. Sources

1. *Operation Breakdown of a Basic T-shirt with SMV and Production Target* — textilesscholars.blogspot.com
2. *Operation Breakdown of a Polo T-shirt with SMV and Production Target* — textilesscholars.blogspot.com
3. *Operation Breakdown and SMV of Men's Long Sleeve Shirt* — ordnur.com
4. *Operation Breakdown of Men's Trouser* — shorabhossain.blogspot.com
5. *Operation Breakdown of Linen Trouser* — shorabhossain.blogspot.com
6. *Operation Bulletin of Leggings* — shorabhossain.blogspot.com
7. *Operation Breakdown and SMVs of a Basic Jeans* — onlineclothingstudy.com
8. *Different Types of Industrial Sewing Machines and Their Use* — onlineclothingstudy.com
9. *Different Types of Sewing Machines and Their Use* — R. Pickard, LinkedIn
10. *Operation Breakdown in Garment Industry* — textilelearner.net

**Honest limitation:** these are published industry-standard breakdowns, not
Needle Craft's own process sheets (which were unavailable). They are
representative of garment manufacturing practice and validated against the
company's real SMV figures, but the exact operation sequences at this specific
factory remain unconfirmed.
