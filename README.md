# quote_converter

## Technical Details:
- This is an Azure Function App written in Python.
    - Python Package Manager: UV
- It is designed to convert supplier quote data into a format suitable for Patriot MRO Solutions.
- The function is triggered by an HTTP request, which contains the supplier quote data in JSON format
- The function processes the data and returns the transformed quote in JSON format.


## Important Notes:
-  markup is applied in code instead of by the model, so the model doesn't need to be prompted about it. This allows for more consistent application of markup across all quotes.


## Examples:

### Example Request Payload to the Function
```json
{
    "file_name": "Template Quote.docx",
    "file_base64": "BASE64_OF_THE_SUPPLIER_FILE",
    "markup_pct": 0.10
}
```
If you want to change markup for a specific quote:
```json
{
    "file_name": "supplier_quote_123.json",
    "file_base64": "BASE64_OF_THE_SUPPLIER_FILE",
    "markup_pct": 0.18
}
```