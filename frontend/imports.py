"""Validate the whole upload before any API request; output the frozen lead shape."""
import csv
import io
import json
import re


class ImportValidationError(ValueError):
    pass


def parse_leads(text, filename):
    try:
        if filename.lower().endswith(".json"):
            rows = json.loads(text)
        elif filename.lower().endswith(".csv"):
            reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
            columns = reader.fieldnames or []
            if (len(columns) != len(set(columns)) or not {"name", "phone"} <= set(columns)
                    or set(columns) - {"name", "phone", "company"}):
                raise ImportValidationError("CSV requires name,phone and optional company columns only.")
            rows = list(reader)
        else:
            raise ImportValidationError("Choose a .json or .csv file.")
    except (json.JSONDecodeError, csv.Error):
        raise ImportValidationError("Invalid JSON or CSV.") from None
    if not isinstance(rows, list) or not rows:
        raise ImportValidationError("Import must be a nonempty list of lead objects.")
    result = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) - {"name", "phone", "company"}:
            raise ImportValidationError(f"Row {index}: only name, phone, company fields are allowed.")
        name, phone, company = row.get("name"), row.get("phone"), row.get("company", "")
        if not all(isinstance(value, str) for value in (name, phone, company)):
            raise ImportValidationError(f"Row {index}: name, phone and company must be strings.")
        name, phone, company = name.strip(), phone.strip(), company.strip()
        if not name or not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
            raise ImportValidationError(f"Row {index}: name required; phone must use E.164 (+country and 8–15 digits).")
        result.append({"name": name, "phone": phone, "company": company})
    return result
