import os
import re
import json
import time
import base64
import logging
from decimal import Decimal, ROUND_HALF_UP

import requests
import azure.functions as func
from jinja2 import Environment, FileSystemLoader, select_autoescape

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


# ---------------------------
# Helpers
# ---------------------------

def money(value):
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        cleaned = str(value).replace("$", "").replace(",", "").strip()
        return float(cleaned)
    except Exception:
        return default


def extract_first(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
    match = re.search(pattern, text or "", flags)
    return match.group(1).strip() if match else ""


def normalize_whitespace(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def html_escape(s):
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------
# Azure Document Intelligence
# ---------------------------

def analyze_document_with_doc_intel(file_bytes: bytes, file_name: str):
    endpoint = os.environ["DOC_INTEL_ENDPOINT"].rstrip("/")
    api_key = os.environ["DOC_INTEL_KEY"]
    model_id = os.environ.get("DOC_INTEL_MODEL", "prebuilt-layout")
    api_version = os.environ.get("DOC_INTEL_API_VERSION", "2024-11-30")

    url = f"{endpoint}/documentintelligence/documentModels/{model_id}:analyze?api-version={api_version}"

    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "Content-Type": "application/octet-stream"
    }

    submit = requests.post(url, headers=headers, data=file_bytes, timeout=90)
    submit.raise_for_status()

    operation_location = submit.headers.get("Operation-Location") or submit.headers.get("operation-location")
    if not operation_location:
        raise RuntimeError("Document Intelligence did not return an operation-location header.")

    for _ in range(60):
        poll = requests.get(operation_location, headers={"Ocp-Apim-Subscription-Key": api_key}, timeout=60)
        poll.raise_for_status()
        result = poll.json()
        status = result.get("status", "").lower()

        if status == "succeeded":
            return result
        if status == "failed":
            raise RuntimeError(f"Document Intelligence analysis failed: {json.dumps(result)}")

        time.sleep(2)

    raise TimeoutError("Timed out waiting for Document Intelligence analysis.")


def table_to_matrix(table_obj):
    row_count = table_obj.get("rowCount", 0)
    col_count = table_obj.get("columnCount", 0)
    matrix = [["" for _ in range(col_count)] for _ in range(row_count)]

    for cell in table_obj.get("cells", []):
        r = cell.get("rowIndex", 0)
        c = cell.get("columnIndex", 0)
        content = normalize_whitespace(cell.get("content", ""))
        matrix[r][c] = content

    return matrix


def find_line_item_table(di_result):
    analyze_result = di_result.get("analyzeResult", {})
    tables = analyze_result.get("tables", [])

    best_table = None
    best_score = -1

    expected_headers = {"line", "item", "description", "unit", "qty", "cost", "lead time"}

    for tbl in tables:
        matrix = table_to_matrix(tbl)
        if not matrix:
            continue

        header_row = {cell.lower() for cell in matrix[0]}
        score = len(expected_headers.intersection(header_row))

        if score > best_score:
            best_score = score
            best_table = matrix

    return best_table


def extract_terms_block(content_text):
    terms_start = re.search(r"Terms and conditions of sale:?", content_text, re.IGNORECASE)
    if not terms_start:
        return []

    block = content_text[terms_start.start():].strip()
    # split on ** style bullets or sentence boundaries
    lines = [normalize_whitespace(x) for x in re.split(r"\n|(?=\*\*)", block) if normalize_whitespace(x)]
    cleaned = []

    for line in lines:
        line = line.replace("**", "").strip()
        if line and line.lower() != "terms and conditions of sale:":
            cleaned.append(line)

    return cleaned


def map_doc_intel_result_to_internal_supplier_quote(di_result, original_file_name="supplier_quote"):
    """
    Maps Azure Document Intelligence layout output into an internal structure
    shaped for the GPT normalization step and final rendering.
    """
    analyze_result = di_result.get("analyzeResult", {})
    content_text = analyze_result.get("content", "")

    # Header fields from full text
    date_value = extract_first(r"Date:\s*(.+?)\s+Quote", content_text)
    project_value = extract_first(r"Project\s+([^\n\r]+)", content_text)
    quote_value = extract_first(r"Quote\s+([^\n\r]+)", content_text)
    ship_to_value = extract_first(r"Ship To:\s*([^\n\r]+)", content_text)
    bid_date_value = extract_first(r"Bid Date\s+([^\n\r]+)", content_text)
    quote_expires_value = extract_first(r"Quote Expires\s+([^\n\r]+)", content_text)
    subtotal_value = extract_first(r"SUBTOTAL\s+\$?\s*([0-9,]+\.\d{2})", content_text)

    # "From" contact block
    from_match = re.search(
        r"From:\s*(.+?)\s+Ph:\s*([^\n\r]+)\s+Email:\s*([^\s]+)",
        content_text,
        re.IGNORECASE | re.DOTALL
    )
    if from_match:
        prepared_by = normalize_whitespace(from_match.group(1))
        prepared_phone = normalize_whitespace(from_match.group(2))
        prepared_email = normalize_whitespace(from_match.group(3))
    else:
        prepared_by = ""
        prepared_phone = ""
        prepared_email = ""

    line_item_matrix = find_line_item_table(di_result)

    line_items = []
    if line_item_matrix and len(line_item_matrix) > 1:
        for row in line_item_matrix[1:]:
            # Skip blank rows
            if not any(normalize_whitespace(cell) for cell in row):
                continue

            row = row + [""] * (7 - len(row))  # pad to 7 cols
            line_no, item, description, unit, qty, cost, lead_time = row[:7]

            qty_num = int(safe_float(qty, 0))
            line_cost = money(safe_float(cost, 0))
            unit_cost = money(line_cost / qty_num) if qty_num else money(0)

            line_items.append({
                "line_no": int(safe_float(line_no, len(line_items) + 1)),
                "supplier_item": item,
                "supplier_description": description,
                "unit": unit,
                "qty": qty_num,
                "supplier_line_cost": float(line_cost),
                "supplier_unit_cost": float(unit_cost),
                "lead_time": lead_time
            })

    mapped = {
        "source_file_name": original_file_name,
        "supplier_quote": {
            "date": date_value,
            "project": project_value,
            "quote_subject": quote_value,
            "ship_to": ship_to_value,
            "bid_date": bid_date_value,
            "quote_expires": quote_expires_value,
            "supplier_subtotal": float(money(safe_float(subtotal_value, 0))),
            "prepared_by": {
                "name": prepared_by,
                "phone": prepared_phone,
                "email": prepared_email
            },
            "line_items": line_items,
            "terms": extract_terms_block(content_text)
        },
        "raw_content_excerpt": content_text[:8000]
    }

    return mapped


# ---------------------------
# Azure OpenAI Structured Outputs
# ---------------------------

QUOTE_NORMALIZATION_SYSTEM_PROMPT = """
You are a quoting transformation assistant for Patriot MRO Solutions.

Your job:
- Read the extracted supplier quote data.
- Normalize product names and descriptions for a customer-facing quote.
- Preserve factual meaning.
- Fix obvious OCR issues and formatting inconsistencies.
- Do NOT invent specifications, quantities, pricing, part numbers, or lead times.
- Do NOT add products that are not present.
- Keep wording professional and concise.
- Terms should be cleaned and preserved where possible.

Important pricing rule:
- You are NOT responsible for markup math.
- The application will calculate customer pricing separately.
- You should only return normalized descriptive content.

Return valid JSON that strictly matches the supplied schema.
""".strip()


QUOTE_NORMALIZATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project": {"type": "string"},
        "quote_subject": {"type": "string"},
        "ship_to": {"type": "string"},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "line_no": {"type": "integer"},
                    "display_item": {"type": "string"},
                    "display_description": {"type": "string"},
                    "unit": {"type": "string"},
                    "qty": {"type": "integer"},
                    "lead_time": {"type": "string"},
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                },
                "required": [
                    "line_no",
                    "display_item",
                    "display_description",
                    "unit",
                    "qty",
                    "lead_time",
                    "notes"
                ]
            }
        },
        "terms": {
            "type": "array",
            "items": {"type": "string"}
        }
    },
    "required": [
        "project",
        "quote_subject",
        "ship_to",
        "line_items",
        "terms"
    ]
}


def call_azure_openai_for_normalization(mapped_supplier_quote: dict):
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    api_key = os.environ["AZURE_OPENAI_KEY"]
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")

    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"

    payload = {
        "messages": [
            {"role": "system", "content": QUOTE_NORMALIZATION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(mapped_supplier_quote, ensure_ascii=False)
            }
        ],
        "temperature": 0.1,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "customer_quote_normalization",
                "strict": True,
                "schema": QUOTE_NORMALIZATION_SCHEMA
            }
        }
    }

    headers = {
        "Content-Type": "application/json",
        "api-key": api_key
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    # Error logging
    if resp.status_code != 200:
        logging.error(f"Azure OpenAI API error: {resp.status_code} - {resp.text}")
    resp.raise_for_status()

    data = resp.json()

    choice = data["choices"][0]["message"]
    content = choice.get("content", "")

    if not content:
        raise RuntimeError(f"Azure OpenAI returned no content: {json.dumps(data)}")

    try:
        return json.loads(content)
    except json.JSONDecodeError as ex:
        raise RuntimeError(f"Azure OpenAI content was not valid JSON: {content}") from ex


# ---------------------------
# Pricing / Markup
# ---------------------------

def build_customer_quote(mapped_supplier_quote: dict, normalized_content: dict, markup_pct: float):
    supplier = mapped_supplier_quote["supplier_quote"]
    markup_multiplier = Decimal("1.00") + Decimal(str(markup_pct))

    final_items = []
    subtotal = Decimal("0.00")

    supplier_item_index = {x["line_no"]: x for x in supplier["line_items"]}

    for item in normalized_content["line_items"]:
        supplier_item = supplier_item_index.get(item["line_no"])
        if not supplier_item:
            continue

        supplier_unit_cost = money(supplier_item["supplier_unit_cost"])
        customer_unit_price = money(supplier_unit_cost * markup_multiplier)
        customer_line_total = money(customer_unit_price * item["qty"])
        subtotal += customer_line_total

        final_items.append({
            "line_no": item["line_no"],
            "display_item": item["display_item"],
            "display_description": item["display_description"],
            "unit": item["unit"],
            "qty": item["qty"],
            "supplier_unit_cost": float(supplier_unit_cost),
            "customer_unit_price": float(customer_unit_price),
            "customer_line_total": float(customer_line_total),
            "lead_time": item["lead_time"],
            "notes": item.get("notes", [])
        })

    customer_quote = {
        "company": {
            "name": os.environ.get("COMPANY_NAME", "Patriot MRO Solutions"),
            "address_1": os.environ.get("COMPANY_ADDRESS_1", "818 Connecticut Ave, Ste 800"),
            "address_2": os.environ.get("COMPANY_ADDRESS_2", "Washington, DC 20006"),
            "phone": os.environ.get("COMPANY_PHONE", ""),
            "email": os.environ.get("COMPANY_EMAIL", ""),
            "prepared_by_name": os.environ.get("DEFAULT_PREPARED_BY_NAME", supplier["prepared_by"].get("name", "")),
            "prepared_by_phone": os.environ.get("DEFAULT_PREPARED_BY_PHONE", supplier["prepared_by"].get("phone", "")),
            "prepared_by_email": os.environ.get("DEFAULT_PREPARED_BY_EMAIL", supplier["prepared_by"].get("email", ""))
        },
        "quote_meta": {
            "date": supplier["date"],
            "project": normalized_content["project"] or supplier["project"],
            "quote_subject": normalized_content["quote_subject"] or supplier["quote_subject"],
            "ship_to": normalized_content["ship_to"] or supplier["ship_to"],
            "bid_date": supplier["bid_date"],
            "quote_expires": supplier["quote_expires"],
            "markup_pct": float(markup_pct)
        },
        "line_items": final_items,
        "subtotal": float(money(subtotal)),
        "terms": normalized_content.get("terms") or supplier.get("terms", [])
    }

    return customer_quote


# ---------------------------
# HTML Rendering
# ---------------------------

def render_html_quote(customer_quote: dict):
    templates_path = os.path.join(os.path.dirname(__file__), "templates")
    env = Environment(
        loader=FileSystemLoader(templates_path),
        autoescape=select_autoescape(["html", "xml"])
    )
    template = env.get_template("quote_template.html")

    logo_base64 = os.environ.get("LOGO_BASE64", "").strip()
    if logo_base64 and not logo_base64.startswith("data:"):
        logo_data_uri = f"data:image/png;base64,{logo_base64}"
    else:
        logo_data_uri = logo_base64

    def fmt_currency(value):
        return f"${safe_float(value):,.2f}"

    def fmt_pct(value):
        return f"{safe_float(value) * 100:.2f}%"

    html = template.render(
        logo_data_uri=logo_data_uri,
        company=customer_quote["company"],
        quote_meta=customer_quote["quote_meta"],
        line_items=customer_quote["line_items"],
        subtotal=fmt_currency(customer_quote["subtotal"]),
        terms=customer_quote["terms"],
        fmt_currency=fmt_currency,
        fmt_pct=fmt_pct
    )

    return html


# ---------------------------
# HTTP Trigger
# ---------------------------

@app.route(route="generate_quote", methods=["POST"])
def generate_quote(req: func.HttpRequest) -> func.HttpResponse:
    """
    POST body (JSON):
    {
        "file_name": "supplier-quote.docx",
        "file_base64": "<base64>",
        "markup_pct": 0.10
    }
    """
    try:
        body = req.get_json()

        file_name = body.get("file_name", "supplier_quote.docx")
        file_base64 = body.get("file_base64")
        markup_pct = float(body.get("markup_pct", 0.10))

        if file_base64 is None:
            return func.HttpResponse(
                json.dumps({"error": "file_base64 is required"}),
                status_code=400,
                mimetype="application/json"
            )

        if markup_pct < 0:
            return func.HttpResponse(
                json.dumps({"error": "markup_pct must be >= 0"}),
                status_code=400,
                mimetype="application/json"
            )

        file_bytes = base64.b64decode(file_base64)

        # 1) Extract with Azure Document Intelligence
        di_result = analyze_document_with_doc_intel(file_bytes, file_name)

        # 2) Map DI result to internal supplier quote structure
        mapped_supplier_quote = map_doc_intel_result_to_internal_supplier_quote(di_result, file_name)

        # 3) Normalize fields/content via Azure OpenAI structured output
        normalized_content = call_azure_openai_for_normalization(mapped_supplier_quote)

        # 4) Apply markup / pricing in deterministic application code
        customer_quote = build_customer_quote(
            mapped_supplier_quote=mapped_supplier_quote,
            normalized_content=normalized_content,
            markup_pct=markup_pct
        )

        # 5) Render HTML
        html = render_html_quote(customer_quote)

        response = {
            "success": True,
            "mapped_supplier_quote": mapped_supplier_quote,
            "normalized_content": normalized_content,
            "customer_quote": customer_quote,
            "html": html
        }

        return func.HttpResponse(
            json.dumps(response, ensure_ascii=False),
            status_code=200,
            mimetype="application/json"
        )

    except Exception as ex:
        logging.exception("Quote generation failed")
        return func.HttpResponse(
            json.dumps({
                "success": False,
                "error": str(ex)
            }),
            status_code=500,
            mimetype="application/json"
        )