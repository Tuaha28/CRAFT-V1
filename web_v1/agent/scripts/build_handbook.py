import json
from pathlib import Path
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.oxml.ns import qn

ROOT=Path(__file__).resolve().parents[1]
pack=json.loads((ROOT/'data/knowledge.json').read_text())
doc=Document()
sec=doc.sections[0]
sec.page_width=Inches(8.5)
sec.page_height=Inches(11)
sec.top_margin=sec.bottom_margin=Inches(.8)
sec.left_margin=sec.right_margin=Inches(.85)
for name in ['Normal','Title','Subtitle','Heading 1','Heading 2']:
    style=doc.styles[name]
    style.font.name='Calibri'
    style.font.color.rgb=RGBColor(0,0,0)
    style.font.size=Pt(11 if name=='Normal' else 15 if name=='Heading 1' else 11 if name=='Heading 2' else 26 if name=='Title' else 13)
    style.paragraph_format.space_after=Pt(8)
doc.styles['Normal'].paragraph_format.line_spacing=1.12
doc.styles['Heading 2'].paragraph_format.space_before=Pt(9)
for element in doc.styles.element.iter(qn('w:pBdr')):
    element.getparent().remove(element)
doc.add_paragraph('Needle Craft Customer Handbook', 'Title')
doc.add_paragraph('Products ordering delivery and customer assistance', 'Subtitle')
doc.add_paragraph('Customer service operating model', 'Subtitle')
doc.add_paragraph('Version 1.0   October 2026')
doc.add_paragraph('This handbook explains the customer journey from a garment enquiry to order delivery and after-sales support. It brings product specifications, ordering rules, sample approvals and service procedures into one reference so that customers receive consistent answers.')
doc.add_paragraph('Use the product code and order reference when discussing a request. General guidance explains the process; an approved quotation and order agreement determine the actual commercial terms. Order progress and shipment estimates are read from the current order system.')
doc.add_paragraph('The customer assistant can answer questions and prepare requests. A submitted request is separate from an approved change, a confirmed order or a verified payment. When the required information is unavailable, the next step is a targeted question or a request to the responsible team.')
doc.add_paragraph('Public company background', 'Heading 2')
doc.add_paragraph('The official website identifies Needle Craft as an apparel group in Jordan. Reference website https://www.ncljo.com/ accessed 12 October 2026. Detailed catalogue values and service rules are maintained as the project reference operating model; their provenance is recorded in the accompanying project documentation.')
doc.add_page_break()
doc.add_paragraph('Contents', 'Heading 1')
for s in pack['sections']:
    doc.add_paragraph(f"{int(s['id'].split('-')[1]):02}  {s['title']}  |  Page {s['page']}")
for s in pack['sections']:
    doc.add_page_break()
    doc.add_paragraph(f"{int(s['id'].split('-')[1]):02} {s['title']}", 'Heading 1')
    for p in s['paragraphs']:
        doc.add_paragraph(p)
    doc.add_paragraph('Common customer questions', 'Heading 2')
    for qa in s['faqs']:
        doc.add_paragraph(qa['question'], 'Heading 2')
        doc.add_paragraph(qa['answer'])
doc.core_properties.title='Needle Craft Customer Handbook'
doc.core_properties.subject='Customer service operating model for the scheduling project'
doc.core_properties.author='Project team'
for element in doc.element.iter(qn('w:pBdr')):
    element.getparent().remove(element)
output=ROOT/'docs/Needle_Craft_Customer_Handbook.docx'
doc.save(output)
print(output)
