# Test with a real Groq key

Groq is separate from xAI Grok. Use https://api.groq.com/openai/v1 and an available Groq model. This patch separates tool planning from the final strict JSON answer because Groq currently does not support tools and structured outputs in the same request.

In VS Code PowerShell, from the project folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.groq.example .env
code .env
```

For an existing environment, edit `.env` instead of overwriting it. Set `LLM_API_KEY` privately to your Groq key. The example uses `openai/gpt-oss-20b`, with strict output support documented by Groq; account access and limits still apply. Embeddings are not configured in this quick test. Retrieval uses the existing lexical knowledge index. Do not run the OpenAI embedding example using a Groq key.

Start the backend:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8001
```

In another terminal:

```powershell
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

In a third terminal:

```powershell
$env:VALIDATION_CUSTOMER_TOKEN="local-customer-1"
.\.venv\Scripts\python.exe scripts\live_validate.py --cases 3 --check-orders
```

This makes real provider calls. Look for mode `llm`, not `offline_fallback`, in the response and preview trace. Results are saved to `reports/live_validation.json`. A technically passing report still needs answer review. No real Groq key was supplied to the build environment; provider compatibility was tested with simulated responses.

Official references:
- https://console.groq.com/docs/openai
- https://console.groq.com/docs/structured-outputs
