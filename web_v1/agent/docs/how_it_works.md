# How the customer agent works

The handbook is the customer-service memory. Order APIs are the source of changing business facts. The language model decides which authorised tool is useful and writes an answer using the returned evidence. The model does not gain direct unrestricted database access.

## A normal knowledge question

A buyer asks, "Can I combine different sizes for the minimum quantity?" The agent searches the prepared handbook chunks. The minimum-order section says sizes can be combined within one style and colour. The agent answers from that section and cites KB-04. It should not invent a special exception if the evidence does not include one.

The 96 FAQs help explain the policies and provide evaluation examples. They are not a fixed menu of the only permitted questions. In LLM mode the agent should also handle paraphrases, relevant follow-ups and multi-part questions. Its quality depends on the knowledge and selected model, so unfamiliar phrasing must be evaluated.

## An order question

A buyer asks, "Where is NC-1042?" The server identifies the customer from the credential, not from a customer ID typed into chat. The order tool checks the account. In the reference database, Customer 1 can see this order and Customer 2 cannot. The answer uses the stored stage, estimate and update timestamp. When connected to the scheduling backend, the same tool fetches those fields over HTTP.

## A request

A buyer says, "Please request 1000 navy T shirts for delivery to Amman." The agent identifies a quote request, collects the specification and prepares a structured action. The interface shows its details. Clicking Confirm submission records the request and gives a reference number. The sales team still needs to approve the commercial quotation. A change request does not silently reschedule factory jobs.

## A question with no reliable answer

A buyer asks for a certificate that is not in the evidence pack. The agent should explain that approved evidence is needed and offer a support request. A public website may help locate background, but it cannot create a missing certificate, validate a payment or grant access to an order.

## Moving to company data

Replace the authored policies with reviewed documents, connect the existing backend, and verify answers against the new sources. The tool names and chat API can remain stable. Update identity handling and permissions, rebuild the index, and run scenarios with different accounts. This lets the FYP demonstrate the complete workflow now while leaving a clear route to real company integration.
