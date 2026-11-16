"""The reference garment families from the customer assistant's handbook (Needle_Craft_Client_Agent).

The agent's own provenance notes mark fabrics, prices and minimum quantities as assumed reference values,
not approved company terms, so every screen shows them as indicative, never as a quotation."""

PRODUCTS = [
    {'style_code': 'NC-TEE-180', 'name': 'Cotton T shirt', 'fabric': '100% cotton jersey', 'gsm': 180,
     'base_price_usd': 4.8, 'moq': 500, 'sizes': 'XS-XXL'},
    {'style_code': 'NC-POLO-220', 'name': 'Cotton blend polo', 'fabric': '65% cotton 35% polyester pique',
     'gsm': 220, 'base_price_usd': 7.2, 'moq': 600, 'sizes': 'XS-XXL'},
    {'style_code': 'NC-ACT-150', 'name': 'Polyester training top', 'fabric': '100% polyester interlock', 'gsm': 150,
     'base_price_usd': 5.6, 'moq': 500, 'sizes': 'XS-XXL'},
    {'style_code': 'NC-HOOD-300', 'name': 'Cotton blend hoodie', 'fabric': '80% cotton 20% polyester fleece',
     'gsm': 300, 'base_price_usd': 15.5, 'moq': 400, 'sizes': 'S-XXL'},
]
