"""Extended customer coverage. Values remain an assumed project operating model."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
RAW=r'''
Company identity location and contacts::
What is the company called?|The public LinkedIn profile uses Needle Craft for Clothing Industry. The project assistant uses Needle Craft as its display name.
Where is Needle Craft located?|The public profile lists 684 Ad-Dulayl Industrial Park QIZ, PO Box 213, Ad-Dulayl 13136, Jordan. Confirm the current visitor or shipping address before travelling or sending goods.
What is the official website?|The public company website is https://www.ncljo.com/. Use approved company channels for private orders and payment instructions.
In which year was the company established?|The website says 2000 while LinkedIn says 2001. Company confirmation is needed; the assistant should not hide the difference.
Who owns or manages the company?|No approved current management profile is supplied. Request the appropriate business contact; do not infer ownership from employee profiles.
Can you give me the current sales phone and email?|A current approved sales contact register is needed. Directories can contain old details. Use the official website or request support rather than inventing contact information.
What are your customer service opening hours?|The project operating model assumes Sunday to Thursday, 08:30 to 17:30 in Jordan local time. These service hours need company approval before real use.
Can I visit the factory without an appointment?|Visits need an agreed appointment and visitor requirements. Submit your company, purpose, preferred date and visitor count for review. A request does not book the visit.
Business customers markets and capabilities::
Do you sell individual garments to consumers?|The reference workflow covers business manufacturing enquiries and bulk orders. Individual retail sales and ready stock are not established capabilities in this pack.
Can a new clothing startup become a customer?|Submit the product brief, quantity and destination. Feasibility, minimum quantities and commercial approval still apply.
Can overseas buyers request production?|Overseas enquiries can be reviewed. Shipping, payment, compliance and destination requirements must be agreed for the actual transaction.
Which countries can you ship to?|There is no approved exhaustive destination list. Provide your destination for logistics review; do not assume unrestricted worldwide delivery.
Do you manufacture uniforms and workwear?|Styles outside the reference catalogue require feasibility review. Protective performance claims require relevant specifications and evidence.
Can I use your garments for my own private label?|Private-label branding can be requested with authorised artwork and approved specifications. Rights, feasibility and pricing need review.
Which famous brands are your current customers?|No approved brand-reference list is supplied. The assistant must not invent relationships or disclose confidential customers.
What is your monthly production capacity?|No approved capacity figure is included. Sales and planning review the style, quantity and date using current capacity; a generic number does not confirm a slot.
New buyer onboarding and enquiry preparation::
What details should I send in my first enquiry?|Company name, style, quantity, fabric or catalogue code, colours, sizes, branding, destination and preferred timeline. A technical pack helps where available.
Can you help me choose a product before I request a quote?|Explain intended use, fabric preference, budget and quantity. The assistant compares reference styles and identifies details needing sales review.
Do I need to register a company account first?|General questions do not require registration. Private order access and submitted requests require verified account access through the main portal.
I only have an idea and no final quantities. What happens next?|The assistant can help structure a brief. A binding quotation and production plan require the final specification and quantity.
What company documents are needed to onboard a buyer?|Use the main portal or sales team's approved checklist. The assistant does not invent mandatory legal documents or collect unnecessary sensitive records.
Can I ask questions before committing to an order?|Yes. An enquiry or discussion is not a purchase commitment. Product and process answers come from the customer knowledge pack.
Can my colleague handle the order for our company?|The colleague needs an authorised account or role assigned in the main portal. An order number alone grants no access.
Can we sign a confidentiality agreement before sharing designs?|Submit the requirement for commercial review. The assistant cannot sign an agreement or claim that chat creates one.
Custom garment design and feasibility::
Can you reproduce a garment from a photograph?|A photograph supports an enquiry but does not define every construction detail. A specification, measurements and sample review are needed.
Can you create a pattern for a new design?|Submit pattern-development requirements for feasibility and costing. This pack does not guarantee that service for every garment type.
Can you make children's garments?|Children's garments are outside the standard adult range. Sizing, safety and destination requirements need a separate review.
Can you make fire resistant protective garments?|No protective performance is promised by the standard catalogue. Material, construction, testing and certification require specialist review.
Can I request an exclusive design?|Exclusivity needs a written commercial agreement. The assistant can collect the requirement but cannot grant exclusive rights.
Who owns the pattern or artwork after development?|Ownership and permitted reuse depend on the agreement. This pack does not establish automatic intellectual-property transfer.
Can I supply my own trims and accessories?|Submit the specification, quantity and suitability requirements, including responsibility for shortages and rejected materials, for review.
Can I change seams or garment construction?|Request changes before approval where possible. They may require resampling, recosting and a revised schedule.
Materials performance and sourcing::
Can you source a fabric that is not in the catalogue?|Provide composition, weight, finish, colour, tests and quantity. Sourcing must confirm availability and timing.
Is a heavier fabric always more durable?|No. Fibre, yarn, construction, finish and intended use affect durability. GSM measures fabric weight, not complete quality.
Are all training tops moisture wicking?|No performance claim follows from a product name. Request the specific finish and test evidence.
Can you guarantee that a fabric is allergy free?|No such guarantee is supported. Material and chemical requirements need evidence and appropriate professional review.
Will the approved fabric ever be substituted automatically?|Substitution requires a reviewed proposal and approval. The original specification remains controlling until a change is accepted.
What happens if a buyer supplied fabric is rejected?|Review inspection results and the agreed responsibility for replacement, cost and timing. The assistant cannot invent liability terms.
Can you provide a fabric swatch before ordering?|Submit a sample or swatch enquiry naming the material. Availability, charges and courier arrangements require confirmation.
Can my order use more than one fabric composition?|Specify materials by style or panel in the pack. Cost, labels and production effects must be reviewed.
Size fit grading and measurements::
Can all sizes have the same unit price?|The approved quotation controls size-based pricing. Reference base values do not guarantee the same price for extended sizes.
Can I use a US or European size chart?|Provide the chart, measurement points, units and tolerances. Size letters alone are not a universal fit specification.
Is chest width the same as chest circumference?|No. A flat garment width and a full circumference are different measurements. Confirm the measurement point.
Can I combine men's and women's fits in one style minimum?|Different fits may count as different styles. Sales must approve how minimum quantities are grouped.
What is grading in garment production?|Grading defines the measurement changes between sizes. The approved chart and rules control the progression.
What if a sample fits but bulk garments do not?|Compare garments with the approved chart and tolerance. Submit the order, quantities, measurements and evidence for quality review.
Can the size labels use my brand's own names?|Request the label wording with the corresponding approved chart. Labelling and destination requirements need final approval.
Can I add a new size after cutting begins?|Prepare a change request. New cutting, materials or samples may be needed; approval and timing are not guaranteed.
Colour artwork branding and packaging approval::
Can the same colour look different on cotton and polyester?|Yes. Fibre, dye, construction and finish affect appearance. Approve standards for the relevant materials.
Can you guarantee a match to a digital logo colour?|A digital image alone cannot guarantee a physical match. Provide a colour standard and approve the sample.
Can a very small logo be embroidered clearly?|Fine details and small lettering may need adjustment. Submit artwork and dimensions for feasibility and sample review.
Can I combine printing and embroidery on one garment?|Submit placement, dimensions and artwork for both. Feasibility, approval, costing and timing need review.
Can you use copyrighted artwork if I send it?|You must be authorised to use it. The assistant cannot establish rights or grant third-party permission.
Can the label text be in more than one language?|Provide required languages and approved wording for layout and destination review. Translation alone does not establish compliance.
Can I change packaging after the order is finishing?|Prepare a change request. Materials, completed packing and schedule determine feasibility.
Who approves the final printed barcode?|The authorised buyer and responsible team approve the specification and verification process. Appearance alone does not certify a barcode.
Samples prototypes and approval followups::
What is the difference between a fit sample and a pre production sample?|A fit sample checks fit and measurements. A pre-production sample checks the agreed bulk specification under the sample plan.
Can the sample charge be deducted from a bulk order?|Only if sales agrees the credit in writing. There is no automatic deduction entitlement.
Who pays to courier the sample?|The sample agreement or quotation states responsibility and charges. Do not assume transport is free.
Can I approve only the colour but reject the fit?|Record separate approval and the outstanding fit issue. Bulk production must satisfy the required approvals.
How many sample revisions are included?|The sample agreement must state included revisions and extra charges. No unlimited-revision policy is established.
Can I ask for a sample in every size?|A size-set sample enquiry can be reviewed. Quantity, charges and timing need confirmation.
What happens if the approved sample is lost?|Confirm the retained approved reference and revision with the team. A replacement or renewed approval may be needed.
Can I approve a sample through the chat alone?|This release can collect a service request. Final sample approval must be recorded through the main system's authorised workflow.
Quotation commercial prices and discounts::
Can you quote the cheapest option for my requirements?|The assistant compares base values and collects requirements. A feasible commercial offer needs sales costing and approval.
Does the displayed price include VAT or import duties?|Reference base prices exclude taxes and duties. The approved quotation states applicable charges and responsibility.
Will larger quantities always lower the unit price?|No automatic discount is established. Materials, operations and total order structure affect costing.
Can I use a previous quote after its validity ends?|Ask sales to reconfirm. Expired pricing and availability cannot be assumed valid.
Can you convert a USD price into Jordanian dinars?|Use an approved current exchange-rate source and timestamp. No live conversion tool is configured; the assistant must not invent a rate.
Can I negotiate the price with the assistant?|The assistant records the target and prepares a request. It cannot grant a discount or accept a counteroffer.
Can you give a final landed cost?|A landed cost needs product charges, freight, insurance, duties, taxes and destination details. A base subtotal is insufficient.
Can a buyer requested rush fee be approved automatically?|Rush feasibility and fees need sales and planning approval. Submission is separate from acceptance.
Purchase orders specifications and documentation::
Can I use my own purchase order reference?|Include it in the description and main portal order process. The company order reference remains the tracking identifier.
Can one purchase order contain several styles?|Provide each style, colour and quantity breakdown. Minimums, approvals and milestones must be clear for every line.
What happens if my purchase order differs from the quote?|Resolve the mismatch before acceptance. The assistant identifies it for review rather than choosing terms.
Can you resend my approved quotation or order confirmation?|Request it through an authorised account or support. This release does not fabricate or directly download missing commercial documents.
Can I upload my technical pack into the chat?|Direct attachment upload is not implemented. Use the main portal's approved upload route and include its document reference.
Can you generate an official invoice from my chat request?|No. Invoices come from the authorised finance system. The assistant can show available invoice fields or request review.
What if two specifications disagree?|Use the latest approved revision linked to the order and resolve remaining conflicts with the team before production.
Can you edit a signed agreement directly?|No. Amendments require authorised agreement. The assistant can prepare a request describing the proposal.
Planning capacity deadlines and disruptions::
Why did the delivery estimate change?|Check the accessible order's current timestamp and customer-facing note. Request a planning update if no reason is recorded; do not invent a disruption.
Can I see an exact production line or machine allocation?|The assistant provides approved customer progress. Internal machine, line and employee allocation stays private.
Can you put my order ahead of another buyer's order?|Request urgent review. The assistant cannot override priorities or disclose another buyer's schedule.
Are weekends and holidays counted in lead time?|Working-day estimates use the scheduling backend calendar. Sampling, manufacturing and transport calendars can differ.
Can you give a live available slot before I submit a purchase order?|A booking requires a dedicated planning API. This release reads milestones and prepares requests; it does not reserve capacity.
What happens if a required material is delayed?|Planning records the effect and an approved customer update or alternative proposal. Substitutions and revised commitments need approval.
Does a progress percentage guarantee the remaining duration?|No. Progress cannot be directly converted to days. Use the latest order-specific estimate and timestamp.
Can you run a what if simulation for my order?|Internal simulation is not exposed by this customer agent. Prepare a planning enquiry for an authorised review.
Fulfilment export freight and delivery exceptions::
Can I collect goods rather than arrange delivery?|Request logistics approval, including collector authorisation and release conditions. A ready stage is not collection permission.
Can part of my order be shipped earlier?|Provide style, quantity and destination for partial-shipment review. Packing, freight and documents may change.
What happens if a tracking number has no carrier update?|Report its recorded reference and last update for logistics review. No update alone does not prove loss.
Can you change the consignee after dispatch?|Submit a logistics request. Carrier and customs restrictions may apply; acceptance is not guaranteed.
Who is responsible for customs clearance?|The agreed delivery basis and contract determine responsibility. Do not assume one party always handles clearance.
Can you insure my shipment through the chat?|Insurance can be requested for review. No purchase or binding insurance-coverage tool is configured.
What if goods arrive with damaged cartons?|Preserve packaging and record delivery details, affected quantity and evidence for logistics and quality review.
Can you guarantee arrival before a public holiday?|No. Production, carrier service and customs must be reviewed before a delivery commitment.
Invoices payments refunds and finance support::
My bank says payment was sent. Is the order paid?|Only a verified finance record confirms receipt. A bank message or screenshot cannot mark the order paid.
Can I pay by card inside the chat?|No payment processing is configured. Use the authenticated main-system process and never paste card details into chat.
Can I change invoice currency after approval?|Request finance review. Currency, conversion and commercial amendments need approval.
Can you change the company name on an invoice?|Request a correction with authorised account details. The assistant does not rewrite an issued invoice.
What happens if I pay less than the invoice amount?|Finance reviews payment, balance and bank charges. The assistant cannot invent a release or write-off policy.
Can you promise the date of a refund?|Refund amount and timing need an authorised finance decision. A complaint or cancellation request is not refund approval.
Someone sent new bank details by message. Should I use them?|Verify instructions through the authenticated approved finance channel. The assistant cannot validate untrusted replacement details.
Can the assistant approve a credit limit?|No. Credit is a finance decision. The assistant can prepare a review request.
Quality complaints remedies and dispute followups::
My shipment has fewer pieces than expected. What information is needed?|Provide order, delivery date, packing-list comparison, styles, quantities and evidence for a discrepancy request.
What counts as a major or minor defect?|Use the agreed classification and inspection plan. The assistant cannot invent a universal acceptance list.
Does an AQL value mean every shipment can contain that percentage of defects?|No. AQL is part of a sampling and acceptance plan, not an unconditional allowed defect percentage.
Can I request replacement instead of a refund?|State your preferred remedy. Responsible teams review feasibility and the agreement before approval.
What if there is an urgent product safety issue?|Mark the request urgent and report affected goods and evidence promptly. The assistant cannot make a legal determination or approve continued use.
Can I dispose of the affected goods before review?|Preserve goods and evidence unless authorised instructions say otherwise. Disposal can prevent assessment.
Can you check the status of an existing support ticket?|Ticket-status lookup needs an additional main-backend endpoint. Include the reference in a follow-up support request; this release does not invent its status.
What if I disagree with the proposed resolution?|Submit the specific disagreement and evidence for further review. The assistant cannot settle the dispute itself.
Reorders repeat buyers and long term supply::
Can I reorder the same style without a new sample?|Sales and quality confirm whether the specification and materials remain valid. Reorders do not automatically waive sampling.
Will a reorder have exactly the same colour as an old batch?|Use the retained approved colour standard and bulk review. An old name or photo cannot guarantee exact shade.
Can I reserve material for future orders?|Reservation, storage, cost and release terms need written agreement. The assistant can collect the requirement.
Can we agree an annual supply programme?|Submit forecasts, quantities, dates and terms for sales and planning. The assistant cannot accept an annual contract.
Can I schedule repeat deliveries every month?|A programme needs approved capacity, releases and terms. No recurring-order execution tool is configured.
Can my repeat order use last year's price?|Sales must reconfirm current costing and validity. Previous prices are not automatically renewed.
Can I duplicate an order directly from chat?|Prepare a reorder enquiry using the accessible reference. An accepted production order still needs the main workflow.
What if the previous style is discontinued or changed?|Sales checks the current specification and reviews alternatives. The assistant must not silently substitute a style.
Customer portal privacy and troubleshooting::
I cannot sign in. Can you reset my password?|Use the portal recovery process. The assistant does not collect passwords or reset credentials.
Can a visitor track an order using only its reference?|No. Private tracking requires an authenticated authorised account.
Why does the assistant say no accessible order was found?|The reference may be wrong or access may be missing. The response deliberately does not reveal whether another buyer owns it.
Can I ask for all information the company keeps about me?|Submit a privacy request to the responsible team. The assistant does not automatically export all system records.
Can you delete my orders immediately?|Deletion and retention need authorised review. Chat cannot delete order history.
Does changing my name in chat change my account identity?|No. Identity comes from the verified credential. Chat cannot switch accounts or grant permission.
What should I do if a request appears twice after retrying?|Keep the original reference and contact support. Confirmation retries use the same action identifier when the backend honours idempotency.
Can I use the assistant in Arabic or Roman Urdu?|A configured multilingual model can use the same knowledge and tools. Language quality requires testing; offline retrieval is primarily English.
Supplier audits certificates and sustainability::
Can you fill my supplier assessment questionnaire?|The assistant can identify known information and missing evidence. It must not invent audit scores, certificates, insurance or financial statements.
Can I receive a current certificate as a PDF?|Request its issuer, programme and scope from the team. The assistant does not generate or certify missing evidence.
Does a certificate cover every factory and every product?|No. Check the covered site, product or material, scope and validity dates.
Can you confirm ethical sourcing from a public directory entry?|A directory or old report alone cannot establish current compliance. Current approved evidence is required.
Can you guarantee that all packaging is biodegradable?|No general claim is supported. Request the specific material and evidence for the actual packaging.
Can you state the carbon footprint of my order?|No approved footprint data or calculation tool is supplied. Request evidence rather than inventing a number.
What if a certificate has expired?|Do not present it as currently valid. Request renewal evidence or scope and date clarification.
Can a web search replace an approved compliance document?|No. Search provides background; transaction-specific claims need approved evidence.
Service requests meetings and assistant boundaries::
Can you arrange a sales meeting?|Prepare a support request with topic and preferred time. A request is not a confirmed calendar booking; there is no meeting tool.
Can you send an email to the sales team automatically?|This release records a request or submits it to the main backend. Email notification needs its configured notification workflow.
Can you call me about my quotation?|Include a callback preference in a reviewed support request. There is no telephone or voice-calling tool in this release.
Can you notify me each time my order stage changes?|Subscriptions and alerts depend on the main portal. This release does not create a notification subscription.
Can you approve a machine repair to speed up my order?|No. Repairs and internal scheduling approvals are staff functions outside customer permissions.
What happens if the order system is temporarily unavailable?|Report lookup failure and the next useful step. Reference records are never used as live fallback when the main backend is configured.
Can you promise an answer to every company related question?|Use knowledge and authorised tools, ask for missing details and escalate unsupported matters. Never invent an answer to appear complete.
Can I confirm several different requests at once?|Review and confirm each prepared action separately. Each has its own identifier; submission and approval are separate.
'''

def extend():
    chunks=[c for c in json.loads((ROOT/'data/chunks.json').read_text()) if int(c['id'].split('-')[1])<=24]
    queries=[q for q in json.loads((ROOT/'data/evaluation_queries.json').read_text()) if int(q['expected_section'].split('-')[1])<=24]
    md='# Extended Customer Questions\n\n'
    i=24
    count=0
    for line in RAW.strip().splitlines():
        if line.endswith('::'):
            i+=1
            title=line[:-2]
            md+='## '+title+'\n\n'
        elif '|' in line:
            question,answer=line.split('|',1)
            count+=1
            sec=f'KB-{i:02}'
            chunks.append({'id':sec,'chunk_id':f'{sec}-Q{count}','title':title,'page':None,'text':title+'\n'+question+'\n'+answer,
                'source':'Extended_Customer_FAQ.md','version':'1.1','updated_at':'2026-10-12','visibility':'public',
                'provenance':'assumed_reference_policy','approval':'prototype_only'})
            queries.append({'question':question,'reference_answer':answer,'expected_section':sec,'category':title})
            md+='**'+question+'**\n\n'+answer+'\n\n'
    (ROOT/'docs/Extended_Customer_FAQ.md').write_text(md,encoding='utf-8')
    (ROOT/'data/chunks.json').write_text(json.dumps(chunks,indent=2),encoding='utf-8')
    (ROOT/'data/evaluation_queries.json').write_text(json.dumps(queries,indent=2),encoding='utf-8')
    print(f'Added {count} extended answers; {len(queries)} total questions; {len(chunks)} chunks')

if __name__=='__main__':
    extend()
