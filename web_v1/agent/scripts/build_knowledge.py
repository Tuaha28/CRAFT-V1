"""Single source for handbook, retrieval records and evaluation queries."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = r'''
Company overview and customer assistance
Needle Craft is an apparel business in Jordan. The company website describes a focus on garment quality, timely delivery and customer satisfaction. The customer assistant supports business buyers with product information, sample enquiries, order tracking and service requests. A visitor can ask general questions without signing in. Private order, invoice and shipment information requires a verified customer account.
The service workflow begins with an enquiry, moves to a specification and quotation, then sampling and approval before production. Production progress and shipment information are obtained from the order system. The assistant explains the information available but cannot guarantee a booking, dispatch date or commercial term that has not been approved. Complex commercial discussions go to the sales team with the customer's consent.
The official public website is https://www.ncljo.com/. Public pages are background sources. Customer-specific agreements take precedence over general guidance where the agreements are available to the authorised service team. Employee records, machine logs, internal costs and other buyers' designs are outside customer access.
Q: What can the customer assistant help me with?
A: Product selection, customisation guidance, sampling, quote requests, your order progress, delivery information and support tickets. It can explain policy and submit requests, but approval comes from the responsible team.
Q: Do I need an account to ask a question?
A: No for general product and policy questions. Sign in before asking about private orders, shipments, invoices or account records.
Q: Can you show another customer's orders?
A: No. An order number does not grant access. The order must belong to the authenticated customer account.
Q: Can I contact a person instead?
A: Yes. Ask for a support ticket and describe your concern. The assistant prepares a request for your review before submission.
===
Product families and product selection
The reference catalogue contains four garment families: cotton T shirts, cotton blend polo shirts, polyester training tops and cotton blend hoodies. Catalogue codes distinguish the exact style and prevent confusion between similar names. NC-TEE-180 is a 180 GSM cotton jersey T shirt. NC-POLO-220 is a 220 GSM cotton polyester pique polo. NC-ACT-150 is a 150 GSM polyester interlock training top. NC-HOOD-300 is a 300 GSM cotton polyester fleece hoodie.
The buyer should choose based on intended use, fabric preference, target weight, fit, quantity and branding requirements. A catalogue description is a starting specification. The approved technical pack and sample define the order. A garment labelled as a training top does not automatically have a tested moisture management, antimicrobial or UV protection claim.
Special garments and materials outside these four styles require a feasibility review. The assistant collects the requirement instead of promising that every design can be manufactured. Images, sketches and a technical pack can support that review. Product availability in this service means a style can be discussed for manufacture; it does not indicate ready stock.
Q: What garments can I enquire about?
A: Cotton T shirts, cotton blend polos, polyester training tops and cotton blend hoodies in the reference catalogue. Other styles require a sales feasibility review.
Q: Which style is suitable for a lightweight sports top?
A: NC-ACT-150 is the catalogue training top at 150 GSM. Any specific performance claim requires a specification and supporting test evidence.
Q: Are these products immediately available from stock?
A: The catalogue supports manufacturing enquiries. Ready stock must be checked separately; do not assume catalogue presence means stock availability.
Q: Can you manufacture a design outside the catalogue?
A: Submit the design, material requirements and quantity for a feasibility review. Acceptance and timing must be confirmed by sales.
===
Fabric composition and material choices
NC-TEE-180 uses 100 percent cotton jersey at a nominal 180 GSM. NC-POLO-220 uses 65 percent cotton and 35 percent polyester pique at 220 GSM. NC-ACT-150 uses 100 percent polyester interlock at 150 GSM. NC-HOOD-300 uses 80 percent cotton and 20 percent polyester fleece at 300 GSM. GSM describes grams per square metre and helps compare fabric weight, but does not by itself prove durability or comfort.
Alternative blends, fabric weights and finishes can be requested. They require material sourcing, costing and sample approval. The exact composition on the quotation, technical pack and care label should agree. Recycled, organic, flame resistant or other special material claims must be supported by valid evidence for the relevant material and order. Do not infer a claim from a catalogue photograph or a product name.
Buyers may supply fabric subject to inspection and a written agreement about suitability, wastage, shortages and responsibility. Fabric substitutions are not automatic. If the agreed material is unavailable, the service team proposes an alternative and obtains approval before updating the specification. Material procurement can affect the production estimate.
Q: What does GSM mean?
A: Grams per square metre, a measure of fabric weight. Higher GSM usually means heavier fabric, not automatically better quality.
Q: What is the polo fabric composition?
A: The reference NC-POLO-220 specification is 65 percent cotton and 35 percent polyester pique at 220 GSM.
Q: Can I request recycled polyester or organic cotton?
A: Yes as an enquiry. Availability, pricing and valid material evidence must be reviewed before the claim appears on an approved order.
Q: Can fabric be changed after approval?
A: A fabric change needs a change request, feasibility review and updated approval. It may affect price, samples and the delivery estimate.
===
Minimum order quantities and quantity planning
The reference minimum order quantity is 500 pieces per style and colour for T shirts and training tops, 600 for polos and 400 for hoodies. Sizes can be combined within one style and colour to meet that minimum. Two different colours are counted separately. For example, 300 black and 300 white T shirts do not meet a 500 piece minimum for each colour.
The minimum is a planning rule for this catalogue. A signed quotation can specify a different agreed quantity. Smaller trial orders are enquiries requiring approval rather than a standard entitlement. Quantities above the minimum do not automatically receive a fixed discount. Sales considers material purchase volumes, operations, trims and total order structure when costing.
The buyer should submit a size breakdown whose total matches the requested quantity. The service team checks totals before sampling and scheduling. Surplus or shortage tolerance must be recorded in the order agreement; there is no default right to ship an unapproved excess. Reorders are reviewed using current sourcing and capacity rather than the previous order's assumptions.
Q: What is the T shirt minimum order quantity?
A: 500 pieces per style and colour in the reference catalogue. Mixed sizes within that style and colour can count towards the minimum.
Q: Can I combine sizes to reach the minimum?
A: Yes within the same style and colour. Submit the size breakdown and ensure the total matches the order quantity.
Q: Can I order 200 hoodies?
A: The reference hoodie minimum is 400 per style and colour. A 200 piece request needs a small-order feasibility review.
Q: Are quantity discounts guaranteed?
A: No. Sales calculates an approved quotation from the specification and quantity. Discounts are not promised by the assistant.
===
Sizing fit and measurement specifications
The reference range is XS through XXL for T shirts, polos and training tops, and S through XXL for hoodies. Availability of other sizes requires review. Size letters alone do not define a universal fit. The approved measurement chart, fit description and sample are the controls for the order. Buyers should specify the intended market, wearer group and preferred fit.
Measurements should identify the point of measurement, units and tolerance. Chest width may be recorded as a flat garment measurement rather than body circumference. Confusing the two can lead to the wrong size recommendation. The assistant explains the chart provided but does not guarantee a customer's body fit from height or weight alone. Buyers should compare an existing suitable garment with the approved chart.
The technical pack records measurements for each size and any grading rules. A 1 centimetre tolerance is a reference discussion value for selected flat measurements; the actual permitted tolerance must be stated in the approved pack. Size labels, multilingual labels and market-specific labels require review. Children's sizing and safety requirements are outside the standard adult catalogue and need separate review.
Q: What sizes are in the catalogue?
A: XS to XXL for T shirts, polos and training tops; S to XXL for hoodies. Other sizes need confirmation.
Q: Are your sizes the same as every retail brand?
A: No. Use the approved measurement chart and fit sample rather than assuming all brands use the same size letters.
Q: Can I change the size breakdown?
A: Submit a change request. Approval depends on the production stage, cutting status and available materials.
Q: Can you guarantee the fit from my height and weight?
A: No. Compare the measurement chart with a suitable garment or request a fit sample for approval.
===
Colours shades and colour approval
Standard catalogue colour names are black, white, navy and grey. A colour name is a description, not a precise colour standard. Buyers should supply a colour reference such as a Pantone code or an approved physical swatch when shade accuracy matters. Screen displays and photographs can differ from the physical fabric.
Custom dyeing requires sourcing review, a colour standard and lab dip approval. Different fibre compositions can produce a different visual result even when the requested shade name is identical. Lab dips and bulk shade bands should be associated with the order revision. Approved colours should be checked under the agreed lighting conditions.
The assistant can collect a requested shade, explain the approval process and submit a colour-change request. It cannot approve a shade on behalf of the buyer. After cutting or dyeing, a colour change may require new material, recosting and a revised delivery estimate. Mixed-colour quantities must meet the minimum for each style and colour unless a written exception exists.
Q: Can I order a custom Pantone colour?
A: Submit the colour reference, garment style and quantity. Custom dyeing requires feasibility and lab dip approval before bulk production.
Q: Is a colour shown on my screen exact?
A: No. Screen appearance is not the approval standard. Use an approved physical swatch or lab dip when colour matching is important.
Q: Can black and navy quantities be combined for the minimum?
A: Normally each style and colour must meet its own minimum. A written exception requires sales approval.
Q: Can I change colour once the order is cutting?
A: A request can be submitted, but approval is not guaranteed. New material and a revised schedule may be needed.
===
Branding printing embroidery and labels
Branding enquiries may include screen printing, heat transfer, embroidery, neck labels, care labels and hangtags. Feasibility depends on the artwork, fabric, placement, size and expected use. The buyer supplies artwork they are authorised to use. The assistant does not approve trademark rights or reproduce another buyer's designs.
Artwork should be supplied as a clean vector file where possible, with dimensions, placement and colour references. A photograph of a logo may be insufficient for production. Embroidery uses a stitch file and can alter the feel of a lightweight fabric. Print tests and sample approval help identify appearance, adhesion and washing issues before bulk production.
Branding affects quotation and lead time. Printing and embroidery are not included in the base product reference prices. Required label text, country-of-origin markings, fibre composition and care instructions should be approved for the destination market. Legal or regulatory wording must be reviewed by an appropriate responsible person rather than improvised by the assistant.
Q: Can I add a logo to a garment?
A: Yes as a branding enquiry. Provide artwork, placement, dimensions, colours, style and quantity for feasibility and costing.
Q: Is printing included in the base reference price?
A: No. Printing, embroidery, special labels and other additions are costed separately in the approved quotation.
Q: Can I use a logo found online?
A: You must have the right to use the artwork. The assistant cannot verify or grant permission for third-party trademarks.
Q: Can you design my care label wording?
A: The team can help collect the required fields. Final wording needs approval against the product and destination-market requirements.
===
Technical packs and order specifications
A technical pack is the controlled description of a garment. It should include the style code, sketch, fabric composition and weight, measurements, colour standard, trims, seams, branding, labels, packaging and quantity breakdown. A complete pack reduces questions during costing, sampling and production. Each revision needs a version identifier and approval history.
If the buyer has no pack, the assistant can collect a structured brief for sales. It should ask for the missing details without creating an approved production specification on its own. Reference photographs can clarify appearance but may leave hidden construction details unresolved. A quotation based on an incomplete brief is provisional until the outstanding details are settled.
The latest approved revision controls production. A message requesting a change does not replace that revision until the service team records approval. The order record links the approved pack, sample and purchase order. Sensitive design files remain private to the owning account. The first release of the assistant accepts text descriptions and document references; direct file ingestion requires the team's approved upload workflow.
Q: What should a technical pack contain?
A: Style, drawings, fabric, measurements, colours, trims, construction, branding, labels, packing and a quantity breakdown, with a revision identifier.
Q: Can I request a quote without a technical pack?
A: Yes. Provide a clear brief and the team can identify missing details. The price and timing remain provisional until specifications are agreed.
Q: Which version controls my order?
A: The latest approved revision linked to the order, not an unapproved message or an old attachment.
Q: Can you share another buyer's technical pack?
A: No. Designs and order documents are private to the authorised customer account.
===
Sampling stages and sample approval
Samples may include an initial development sample, a fit sample, a colour or print approval sample and a pre-production sample. Not every order requires every stage. The sample plan is recorded during quotation and depends on whether the style and specifications are already established. Bulk production starts only after the required approvals and commercial checks.
The reference development sample target is 7 to 10 working days after complete specifications, available material and any required sample payment. This is an estimate, not a confirmed service promise. Special materials, custom dyeing or repeated revisions can take longer. Sample charges and courier charges are quoted separately; free samples are not a default policy.
Approval should identify the sample reference, revision and any accepted deviations. Silence is not approval. Feedback should be specific about measurements, shade, construction or branding. When several issues are reported, the team consolidates them into the next revision. The assistant can prepare a sample request and explain status if the backend provides it, but cannot declare a sample approved without a recorded decision.
Q: How long does a development sample take?
A: The reference target is 7 to 10 working days after the brief, materials and required payment are ready. Sales must confirm the actual estimate.
Q: Are samples free?
A: Sample and courier charges are quoted separately. Free samples or charge credits need a written commercial agreement.
Q: Can production begin before sample approval?
A: Only if the responsible team records an approved exception. The assistant cannot grant that exception.
Q: How should I approve a sample?
A: Confirm the sample reference and revision explicitly, and list any agreed deviations. Silence or a vague acknowledgement is not approval.
===
Quotation requests and reference pricing
A quotation request should include style, quantity, colour, size breakdown, branding, delivery destination and any required date. Sales uses these details to review feasibility and issue a commercial quotation. The agent can collect and submit the request but does not issue a binding offer. A request number confirms submission, not commercial acceptance.
The reference base prices in USD are 4.80 for NC-TEE-180, 7.20 for NC-POLO-220, 5.60 for NC-ACT-150 and 15.50 for NC-HOOD-300 per piece. These values support indicative calculations for the reference catalogue. They exclude freight, taxes, duties, samples, branding, special packaging and nonstandard materials. Multiplying quantity by a base value produces a subtotal only, not a total payable amount.
An approved quotation records currency, quantities, unit prices, additions, shipping term, payment conditions and validity. The reference quotation validity is 14 calendar days unless the document states otherwise. A rate from a previous order cannot automatically be applied to a new order. Currency conversions should use a known exchange-rate source and timestamp; the assistant should not invent a rate.
Q: What is the base price of a T shirt?
A: NC-TEE-180 has a reference base price of USD 4.80 per piece, excluding additions, freight, duties and taxes. A binding price requires a sales quotation.
Q: What is the reference subtotal for 1000 T shirts?
A: 1000 times USD 4.80 equals USD 4800 for base garments only. Branding, shipping and other applicable charges are separate.
Q: How long is a quotation valid?
A: The reference validity is 14 calendar days unless the approved quotation specifies a different period.
Q: Can the assistant confirm my final price?
A: It can provide an indicative catalogue calculation or show an available approved quotation. It cannot turn a request or estimate into a binding price.
===
Placing orders and commercial acceptance
The order process starts with a reviewed quotation and purchase order. The buyer confirms the specification, quantities, delivery destination and commercial terms. The service team then checks sample approval, material readiness and the agreed payment condition before accepting the order into the production workflow. Creating a quote request alone does not place an order.
The accepted order receives a unique reference linked to the customer account. The confirmation should state the approved specification revision, quantity, currency, delivery basis and estimated milestones. Any uncertain fields should be recorded as pending rather than treated as agreed. A requested delivery date is a buyer preference until the production and logistics teams confirm feasibility.
Customer identity and account details are checked using the existing customer portal. New accounts require the main system's registration process. The assistant cannot create privileged accounts, accept payment, sign contracts or approve purchase orders in the first release. It can explain the next steps and submit a support request when a confirmation is missing. Buyers should retain the approved documents for future reference.
Q: Does submitting a quotation request place an order?
A: No. Sales reviews the request first. An order requires commercial acceptance and the agreed specification and approval steps.
Q: What do I need to confirm an order?
A: The reviewed quotation, purchase order, approved specification and sample requirements, delivery details and agreed payment condition.
Q: Is my requested delivery date guaranteed?
A: No. It becomes a commitment only through the responsible team's approved order confirmation.
Q: Can the assistant sign a contract for me?
A: No. Contracts and purchase-order acceptance are handled by authorised people through the company's commercial process.
===
Payments deposits and invoice enquiries
The reference payment arrangement for a new buyer is 30 percent advance and 70 percent before dispatch, subject to the signed quotation. Existing customers may have different approved terms. The assistant explains the recorded arrangement but does not negotiate credit, change due dates or approve an exception. Payment instructions should come from an authenticated approved invoice or finance channel.
For example, an approved USD 4800 garment subtotal would have a USD 1440 advance and USD 3360 balance under a 30 and 70 arrangement, before any additional charges. This calculation does not establish an invoice or prove a payment has been received. Only the finance system can report payment verification. The assistant must not mark an order paid based on a customer message or screenshot.
Private invoice information belongs to the customer account. The assistant may show invoice number, currency, amount and payment status if the backend supplies those fields. It must not disclose another buyer's invoice or invent bank account details. Disputes, suspected incorrect instructions and requests for changed payment terms should become finance support tickets for review.
Q: What are the reference payment terms?
A: For a new buyer, 30 percent advance and 70 percent before dispatch, unless the signed quotation sets different terms.
Q: Can you confirm that my payment arrived?
A: Only a verified finance-system record can confirm receipt. If that information is unavailable, request a finance review.
Q: Can you provide a bank account number?
A: Use authenticated approved payment instructions from finance. The assistant does not invent or accept replacement bank details from chat.
Q: Can I request extended payment terms?
A: Yes through a finance support request. Credit or payment-term changes require authorised approval.
===
Lead times calendars and date estimates
The reference bulk manufacturing estimate is 25 to 35 working days after all required approvals, materials and commercial checks are complete. Special materials, custom dyeing, complex branding and large quantities can change that estimate. Sampling, transport and customs time are separate. A manufacturing estimate should never be described as a guaranteed arrival date.
The reference working calendar is Sunday to Thursday, with Friday and Saturday treated as nonworking days for planning. Holidays and shift changes are recorded in the scheduling system. This calendar is a planning configuration and should be replaced if the factory calendar differs. The backend is responsible for actual date calculations because it knows current capacity and holidays.
The assistant asks which order and destination the customer means. It reads the order's current estimate and timestamp rather than adding arbitrary days to today's date. When a schedule changes, the reason given to the customer should be an approved customer-facing summary such as a material delay, not private employee or machine information. Rush requests require feasibility review and may affect cost.
Q: What is the bulk production lead time?
A: The reference estimate is 25 to 35 working days after required approvals, materials and commercial checks, excluding sampling and shipping. Confirm the order-specific estimate.
Q: Does manufacturing lead time include shipping?
A: No. Transport and customs time are separate and depend on the agreed delivery basis and destination.
Q: Can you guarantee delivery next week?
A: No. The scheduling and logistics teams must check feasibility before an arrival date can be committed.
Q: Which calendar should determine my order estimate?
A: The current factory calendar in the scheduling backend, including holidays and approved shifts, controls the live estimate.
===
Order stages and progress tracking
Customer-facing stages are awaiting approval, material preparation, cutting, stitching, finishing, quality inspection, ready for dispatch, shipped and delivered. A stage describes the current milestone and may not represent a fixed percentage of total elapsed time. Percentage complete should be supplied by the production system and must have a clear definition.
The assistant retrieves order status using the authenticated account and order reference. A response can include style, quantity, stage, progress percentage, estimated ready date, estimated arrival date and last update time. It should identify an estimate as an estimate. A stale update is not proof that no production occurred; the customer may need a service-team check.
Customers may ask why an order has changed stage or appears delayed. The assistant shares only the customer-facing reason supplied by the backend. It does not reveal staff names, machine failures, internal utilisation, another buyer's priority or confidential production costs. If the backend is unavailable, the assistant says that live status could not be checked and offers a support request rather than inventing a stage.
Q: Where is my order?
A: Sign in and provide the order reference. The assistant checks that account's live order record and returns the available stage and estimates.
Q: Does 50 percent complete mean half the remaining days?
A: No. Progress depends on how the production system measures completion. It cannot be directly converted into days remaining.
Q: Can you show the machines working on my order?
A: Customers receive approved order progress. Internal machine and employee records are not exposed through the customer assistant.
Q: What if order tracking is unavailable?
A: The assistant should report the temporary lookup failure and offer a service ticket. It must not fabricate live progress.
===
Order changes cancellation and reprioritisation
Customers can request a quantity, size, colour, branding, address or timing change. Every change is reviewed against the order's stage, committed materials and approved specification. The assistant records the proposed change as a request; it does not directly alter production or promise approval. Cutting, printing and completed operations can make changes difficult or costly.
Before submission, the assistant shows the order reference and the exact requested change for confirmation. The backend stores the request and routes it to the responsible team. An accepted change may require a revised quotation, sample, technical pack and schedule. Until approval is recorded, the existing order remains the controlling agreement.
Cancellation enquiries follow the same review process. Recoverable costs depend on work and materials already committed. There is no automatic promise of a full refund after production begins. A delivery-address change after dispatch requires logistics review and may be restricted by the carrier. Rush requests are not guaranteed to move ahead of other orders. The customer receives the request reference for tracking.
Q: Can I change the colour of my order?
A: You can submit a colour-change request. Approval depends on the current stage, material commitments and a reviewed effect on cost and timing.
Q: Does submitting a change update the production schedule?
A: No. The request is pending until the responsible team approves and the scheduling backend records the change.
Q: Can I cancel after cutting has started?
A: Submit a cancellation enquiry. The team reviews committed costs and possible cancellation terms; a full refund is not guaranteed.
Q: Can I request earlier delivery?
A: Yes through a rush or timing-change request. Capacity, materials and logistics must be reviewed before approval.
===
Quality checks inspection and acceptance
Quality checks may include incoming material inspection, in-process checks, measurement review, branding checks and final inspection. The order's approved sample and technical pack define the acceptance criteria. The assistant can explain the workflow but cannot issue an inspection certificate or claim that every unit was individually inspected.
If an order uses an AQL inspection plan, the sampling level, defect categories and limits should be stated in the agreement. The reference discussion values are 2.5 for major defects and 4.0 for minor defects, subject to the approved inspection plan. These are not promises that a fixed percentage of defects is acceptable in every shipment. Safety-critical concerns require immediate escalation.
A buyer may request third-party inspection, with booking, scope and cost agreed before dispatch. Inspection status should come from the order or quality system when available. Reports belong to the customer account. A complaint should include the affected style, quantity, batch, photographs and a clear description. Disagreement about acceptance is handled by the quality and commercial teams rather than decided by the assistant.
Q: What defines acceptable garment quality?
A: The approved technical pack, sample, measurements and agreed inspection plan define acceptance for the order.
Q: What is the reference AQL discussion value?
A: 2.5 for major defects and 4.0 for minor defects, only if the order's approved inspection plan adopts them.
Q: Can I arrange a third-party inspection?
A: Request it before dispatch. The service team must agree the inspection scope, scheduling and responsibility for costs.
Q: Can the assistant approve a failed inspection?
A: No. Quality acceptance, rework and concessions require authorised review and a recorded decision.
===
Packaging carton information and labels
The reference packing method is one garment per protective bag, with size and style identification, grouped into export cartons. The actual packing specification may differ by buyer or destination. Carton assortment, maximum weight, dimensions, barcodes and shipping marks should be specified and approved before packing.
Custom packaging, recycled packaging claims, retail-ready presentation and pallet requirements affect materials and cost. The assistant collects the requirement rather than stating that all packaging options are available. Packaging claims require supporting evidence where relevant. The final packing list records actual quantities and carton counts after packing; a pre-production estimate is not a final packing list.
For shipment planning, customers may ask for gross weight, net weight or carton dimensions. These values should come from the logistics record. The assistant must not calculate actual carton weight from garment GSM alone. A customer-specific label or barcode file remains private. Changes to packaging after materials have been ordered require a change review, and a revised dispatch estimate may be necessary.
Q: What is the reference packing method?
A: Individual protective bags with style and size identification, grouped into export cartons, unless an approved buyer specification says otherwise.
Q: Can I request custom barcodes and carton marks?
A: Yes. Provide the format and placement requirements for review before packing materials are prepared.
Q: Can you give my final carton count before packing?
A: Only an estimate may be available. The final count comes from the completed packing and logistics record.
Q: Does garment GSM tell me the shipping weight?
A: No. Actual shipping weight also depends on garment dimensions, trims, packaging and carton configuration.
===
Shipping freight and delivery tracking
Shipping can be arranged by the buyer or reviewed with the service team. The approved quotation identifies the delivery basis and which party arranges freight. Air, sea and road options depend on the destination, shipment size and available carriers. The assistant cannot assume a route or carrier from general company information.
Shipping estimates should identify the carrier or logistics source, destination, service, estimate date and whether customs time is included. An estimated arrival is not a guarantee. A production-ready date is different from dispatch and arrival dates. Shipment tracking comes from the order's shipping record, including a carrier and tracking reference when available.
If there is no tracking number, the agent should state the current recorded stage rather than invent a link. A late shipment becomes a logistics support request with the order and concern. Address changes after dispatch, partial shipments, split destinations and special handling need review. No customer should receive another buyer's shipping documents. The first release reads shipping fields from the order backend rather than querying every carrier directly.
Q: How can I track a shipment?
A: Sign in and provide your order reference. The assistant checks recorded shipment status and the carrier tracking reference if available.
Q: Is the production-ready date my delivery date?
A: No. Dispatch, transport and customs may occur after production is ready. Use the order's separate delivery estimate.
Q: Can I split an order across destinations?
A: Submit the destination and quantity breakdown for logistics review. Packing, freight and documentation may change.
Q: Can you promise a specific carrier transit time?
A: Only quote a current carrier or logistics estimate with its source. Arrival remains subject to the shipment and customs process.
===
Export documents duties and destination requirements
Common shipment documents may include a commercial invoice, packing list, transport document and certificate of origin where applicable. The required set depends on the destination, garment, transaction and delivery arrangement. The responsible export team confirms the documents for the shipment. The assistant can collect a document request, but cannot certify origin or issue official customs documents.
Buyers should identify the destination country and any required labelling, testing or import paperwork during quotation. Duties, taxes and eligibility for trade preferences depend on current rules and the actual goods. The assistant must not promise duty-free treatment or rely on old tariff percentages from a public profile. A customs broker or responsible export professional should verify transaction-specific requirements.
Product descriptions and codes must match the actual garment and approved documents. The assistant should not choose a legally binding classification from a short chat description. Shipping terms and responsibility for import clearance should be clear in the quotation. Private commercial invoices and transport documents require account-level access. Unavailable documents become a support request to the export team.
Q: Which export documents can be requested?
A: Commercial invoice, packing list, transport document and applicable origin documents, subject to the shipment and destination requirements.
Q: Are all orders automatically duty free?
A: No. Eligibility depends on current trade rules, product origin and the transaction. Confirm with the export team or customs broker.
Q: Can the assistant certify country of origin?
A: No. Origin certification requires the responsible authorised process and evidence.
Q: Can you tell me the exact import duty for my shipment?
A: The destination, classification, origin and current rules must be checked by a responsible professional. Do not treat a general web result as a binding assessment.
===
Claims defects returns and commercial remedies
The reference reporting window is 7 calendar days after receipt for visible quantity or quality concerns. Report the order reference, delivery date, affected styles and quantities, batch details and clear evidence. Hidden defects or safety concerns should still be reported as soon as discovered and reviewed under the applicable agreement; the assistant should not automatically reject them based on the visible-defect window.
The buyer should preserve the affected goods and packaging while the team reviews the issue. Do not return goods without return authorisation and shipping instructions. A complaint submission does not approve replacement, refund, discount or reimbursement. The quality and commercial teams review the evidence and propose a resolution consistent with the agreement.
Custom manufactured goods do not have an automatic change-of-mind return entitlement under this reference service process. This does not override a signed agreement or applicable rights. The assistant does not provide a legal determination. It can submit a support ticket, show the confirmation and explain which details help review. Safety incidents should be marked urgent and routed to a responsible person.
Q: How do I report defective garments?
A: Provide your order, affected quantity, delivery date, description and evidence. The assistant prepares a quality support request for confirmation.
Q: What is the visible-defect reporting window?
A: The reference window is 7 calendar days after receipt. Hidden defects and safety issues still need prompt review under the applicable agreement.
Q: Can I send goods back immediately?
A: Wait for return authorisation and shipping instructions. An unapproved return may complicate review and costs.
Q: Is a refund automatic when I create a complaint?
A: No. Refund, replacement, rework or another remedy requires review and an authorised decision.
===
Garment care storage and washing advice
The approved care label for the actual garment is the primary care instruction. A generic fabric description is not enough to determine a safe washing temperature, drying method or ironing setting. Printing, embroidery, trims and finishes can change care requirements. The assistant should ask for the style or care-label text before giving detailed advice.
General handling guidance is to separate colours, avoid prolonged damp storage and protect printed areas from direct high heat unless the label permits it. For an unfamiliar finish, trial washing of an approved sample may help the buyer assess appearance. The assistant must not claim that a product is shrink-proof, colourfast or suitable for industrial laundry without appropriate evidence.
If washing produces unexpected fading, shrinkage or print damage, the buyer should retain the garment and describe the wash cycle, detergent, temperature and drying method. The quality team compares those details with the approved care instructions and specification. Storage before retail should keep cartons dry and protected from contamination. Product-specific care instructions should be added to the knowledge base when approved.
Q: How should I wash my garments?
A: Follow the actual approved care label. Share its text or your style reference if you need help understanding it.
Q: Are all cotton garments shrink-proof?
A: No. Shrinkage performance depends on fabric, processing and care. A guarantee needs approved specifications and test evidence.
Q: Can I iron directly over a printed logo?
A: Follow the approved care label and print instructions. Avoid direct high heat on printing unless explicitly permitted.
Q: What information helps a washing-related complaint?
A: Style, order, wash temperature, detergent, cycle, drying method, observed damage and supporting photographs.
===
Certifications sustainability and evidence requests
Customers may ask about social compliance, factory audits, material certification or environmental claims. The agent should only name a certification when a current approved document identifies the issuer, scope, covered site or product and validity period. Public membership, participation in a programme and a product certification are different things.
The reference knowledge pack does not supply an active certificate register. The correct response to a request for certified organic, recycled or other certified production is to collect the exact requirement and request evidence from the responsible team. The assistant must not invent certificate numbers, audit scores, brand relationships or sustainability achievements.
Customers may request an evidence pack during supplier assessment. That request should state the programme, site, product and intended use. Some evidence may require controlled sharing or a confidentiality agreement. Historical reports and third-party directories should not be presented as proof of current certification. External web search can help locate public information, but internal approved evidence determines what the company can claim for an order.
Q: Which certifications are currently valid?
A: A current approved certificate register is required. No active certification should be claimed without issuer, scope and validity evidence.
Q: Does programme membership certify every product?
A: No. Membership or participation is different from certification of a specific site, material or product.
Q: Can I request sustainability evidence?
A: Yes. Specify the material, programme, product and site requirement so the responsible team can provide suitable evidence.
Q: Can the assistant invent a certificate for my supplier form?
A: No. Supplier forms must use valid approved evidence or clearly state that the evidence is not yet available.
===
Accounts confidentiality and customer permissions
General visitors can access approved public product and policy information. Authenticated customers can access records belonging to their account. The backend determines identity; a name, email or customer ID typed into chat cannot override it. If an order reference does not belong to the signed-in account, the assistant should report that it could not find an accessible record without revealing whether another buyer owns it.
Account registration, password resets and user permissions are handled by the main portal. The first release of the agent does not change credentials or grant access. Customers should not paste passwords, payment-card details or sensitive authentication codes into chat. Support requests should contain only the information needed to resolve the concern.
Conversation history and service actions should be retained according to an approved company policy. The reference project does not establish a legal retention period. Access or deletion requests go to the responsible privacy or account team. Internal documents are excluded from the customer index unless explicitly approved for customer viewing. Web search uses a redacted public query; private order contents should not be sent to external search providers.
Q: Can I access an order by knowing its number?
A: No. The authenticated account must be authorised for that order. The number alone does not grant access.
Q: Can I change my password through the agent?
A: Use the main portal's account-recovery process. The agent does not collect passwords or grant account permissions.
Q: Can customers see internal production data?
A: Only approved customer-facing progress is shared. Employee information, machine logs, costs and other buyers' data stay private.
Q: Can I request deletion of account information?
A: Submit an account or privacy support request. The responsible team reviews it under the applicable approved process.
===
Support escalation and service boundaries
The assistant handles knowledge questions, reads accessible order information and prepares quotation, sample, order-change and support requests. It asks focused follow-up questions when required fields are missing. It never claims that a submitted request is approved. Confirmation displays the action details and creates a request reference only after the system records the submission.
The reference service target for an initial support response is one working day, excluding urgent incidents and cases needing specialist review. This is a planning target, not a contractual response guarantee. Urgent quality or safety concerns should be identified in the ticket and brought to the responsible team. Financial disputes, legal requirements and unsupported technical claims require specialist review.
If evidence is missing or tools fail, the assistant should describe the limit in plain language and offer the next useful step. It can search approved public sources for general information when configured, but a website cannot approve a private order change. Greetings, off-topic requests and abusive messages should receive an appropriate brief response. The customer service scope does not include arbitrary tasks unrelated to the company or unrestricted access to the factory system.
Q: How soon should support respond?
A: The reference initial-response target is one working day. Complex cases and specialist reviews may take longer; this is not a contractual guarantee.
Q: What happens when the assistant has no answer?
A: It explains the missing information, asks for useful details or offers a support request instead of inventing a policy or status.
Q: Will a submitted request be automatically approved?
A: No. Submission and approval are separate. The responsible team reviews commercial or operational requests.
Q: Can the agent perform any task I ask?
A: It can use its authorised customer-service tools. It cannot perform unrelated tasks, approve contracts or bypass account and factory permissions.
'''

def build():
    sections, chunks, queries = [], [], []
    for i, raw in enumerate(RAW.strip().split('\n===\n'), 1):
        lines = raw.strip().splitlines()
        title = lines[0]
        paragraphs, faqs = [], []
        for line in lines[1:]:
            if line.startswith('Q: '):
                faqs.append({'question': line[3:], 'answer': ''})
            elif line.startswith('A: '):
                faqs[-1]['answer'] = line[3:]
            elif line.strip():
                paragraphs.append(line)
        sec = {'id': f'KB-{i:02}', 'title': title, 'page': i + 2,
               'paragraphs': paragraphs, 'faqs': faqs, 'version': '1.0',
               'updated_at': '2026-10-12', 'visibility': 'public',
               'provenance': 'assumed_reference_policy', 'approval': 'prototype_only'}
        sections.append(sec)
        for j, paragraph in enumerate(paragraphs, 1):
            chunks.append({**{k:v for k,v in sec.items() if k not in ('paragraphs','faqs')},
                           'chunk_id': f'KB-{i:02}-P{j}', 'text': title+'\n'+paragraph})
        for j, qa in enumerate(faqs, 1):
            chunks.append({**{k:v for k,v in sec.items() if k not in ('paragraphs','faqs')},
                           'chunk_id': f'KB-{i:02}-Q{j}', 'text': title+'\n'+qa['question']+'\n'+qa['answer']})
            queries.append({'question':qa['question'], 'expected_section':sec['id'],
                            'reference_answer':qa['answer'], 'category':title})
    pack = {'name': 'Needle Craft customer knowledge', 'version': '1.0',
            'source_document': 'Needle_Craft_Customer_Handbook.docx', 'sections':sections}
    for name, value in [('knowledge.json',pack),('chunks.json',chunks),('evaluation_queries.json',queries)]:
        (ROOT/'data'/name).write_text(json.dumps(value,indent=2), encoding='utf-8')
    markdown = '# Needle Craft Customer Handbook\n\n'
    for sec in sections:
        markdown += '## '+sec['id']+' '+sec['title']+'\n\n'+'\n\n'.join(sec['paragraphs'])+'\n\n'
        for qa in sec['faqs']:
            markdown += '**'+qa['question']+'**\n\n'+qa['answer']+'\n\n'
    (ROOT/'docs'/'customer_handbook.md').write_text(markdown, encoding='utf-8')
    print(f'{len(sections)} sections, {len(chunks)} chunks, {len(queries)} evaluation questions')
    from build_extended_faq import extend
    extend()

if __name__ == '__main__':
    build()
