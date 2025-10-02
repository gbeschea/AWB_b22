*** Begin Patch
*** Update File: address_service.py
@@
-async def validate_address_for_order(db: AsyncSession, order: Any) -> ValidationResult:
-    in_judet_raw = getattr(order, "shipping_province", None) or ""
-    in_city_raw  = getattr(order, "shipping_city", None) or ""
-    in_zip       = (getattr(order, "shipping_zip", None) or "").strip() or None
-    in_addr1     = getattr(order, "shipping_address1", None) or ""
-    in_addr2     = getattr(order, "shipping_address2", None) or ""
-
-    errors: List[str] = []
-    suggestions: List[str] = []
-    score = 100
-
-    if detect_easybox(in_addr1, in_addr2):
-        msg = "Adresă locker (easybox/pick-up) detectată — considerată validă."
-        _set_order_fields(order, "valid", 100, [msg], [])
-        return ValidationResult(True, 100, [msg], [])
-
-    # București: sectorul face județ/localitate = București (sectorul rămâne opțional)
-    in_judet, in_city, detected_sector = bucharest_fix(in_judet_raw, in_city_raw, in_addr1, in_addr2)
-    if detected_sector and (norm_text(in_judet_raw) != "bucuresti" or norm_text(in_city_raw) != "bucuresti"):
-        suggestions.append(f"Recomandat: Județ='București' și Localitate='București' (sector {detected_sector}).")
-
-    # SAT/comună din text
-    settlements = detect_settlements(in_addr1, in_addr2)
-    prefer_loc = None
-    sat_names = [name for kind,name in settlements if kind.startswith("sat")]
-    if sat_names: prefer_loc = sat_names[0]
-    elif settlements: prefer_loc = settlements[0][1]
-
-    # Extrage stradă/număr
-    street1 = street_core(in_addr1)
-    street2 = street_core(in_addr2)
-    chosen_street = street1 if len(street1) >= len(street2) else street2
-    chosen_number = _has_real_house_number(in_addr1) or _has_real_house_number(in_addr2)
-
-    # Reguli business minime
-
-    # Heuristic: if the "street" is actually a 6-digit number (a likely ZIP code) raise a clearer error
-    if not chosen_street:
-        for _p in (in_addr1, in_addr2):
-            tok = (str(_p or "").strip())
-            if tok and tok.isdigit() and len(tok) == 6:
-                errors.append("Câmpul stradă pare să conțină un cod poștal, nu o denumire de stradă.")
-                break
-    if not chosen_street:
-        errors.append("Strada lipsește sau nu poate fi determinată."); score -= 60
-    if not chosen_number:
-        errors.append("Numărul stradal lipsește din adresă."); score -= 60
-    if not in_judet:
-        errors.append("Județul lipsește din adresă."); score -= 40
-    if not in_city_raw:
-        errors.append("Localitatea lipsește din adresă."); score -= 40
-    hard_invalid = score < 60
-
-    # Candidați (J+L) și din ZIP
-    jl_rows = await _load_candidates_for_locality(db, in_judet, in_city_raw)
-    zip_rows = await _load_by_zip(db, in_zip) if (in_zip and re.fullmatch(r"\d{6}", str(in_zip))) else []
-
-    # Regula 2 din 3
-    pair_ok = {"JL": bool(jl_rows), "JZ": False, "LZ": False}
-    if zip_rows:
-        j_owner, l_owner = _zip_owner_stats(zip_rows)
-        if j_owner and same_locality(j_owner, in_judet): pair_ok["JZ"] = True
-        if l_owner and same_locality(l_owner, in_city_raw): pair_ok["LZ"] = True
-
-    # Recomandări pe baza disonanțelor ZIP
-    if pair_ok["JL"] and not (pair_ok["JZ"] and pair_ok["LZ"]):
-        z_s = _candidate_zip_from_jl(jl_rows, in_addr1 or in_addr2, chosen_number)
-        if z_s and (not in_zip or str(in_zip) != z_s):
-            suggestions.append(f"cod_postal_sugerat={z_s}")
-        if zip_rows:
-            detail = _zip_best_match_detail(zip_rows, in_addr1 or in_addr2, chosen_number)
-            if detail:
-                suggestions.append(f"Stradă/interval probabil pentru ZIP: {detail}")
-
-    # Decizie pe baza scorului și a regulii 2 din 3
-    if hard_invalid or not (pair_ok["JL"] or (pair_ok["JZ"] and pair_ok["LZ"])):
-        _set_order_fields(order, "invalid", max(0, score), errors, suggestions)
-        return ValidationResult(False, max(0, score), errors, suggestions)
-
-    _set_order_fields(order, "valid", max(0, score), [], suggestions)
-    return ValidationResult(True, max(0, score), [], suggestions)
+async def validate_address_for_order(db: AsyncSession, order: Any) -> ValidationResult:
+    """ZIP-first, regulă strictă pe număr și validare pe baza nomenclatorului din DB.
+
+    Schimbări majore față de v8.3:
+      • elimină „2 din 3 (J/L/ZIP)” → folosim **ZIP ca sursă canonică**;
+      • **numărul stradal este obligatoriu** (exceptând easybox);
+      • potrivirea J/L cu ZIP devine condiție de validitate (cu sugestii de corecție);
+      • străzile numeronime (ex. „1 Mai”, „13 Septembrie”, „1 Decembrie 1918”) nu sunt tratate ca numere;
+      • numerele din Bl/Sc/Ap/Et nu sunt considerate număr stradal.
+    """
+
+    in_judet_raw = getattr(order, "shipping_province", None) or ""
+    in_city_raw  = getattr(order, "shipping_city", None) or ""
+    in_zip_raw   = (getattr(order, "shipping_zip", None) or "").strip()
+    in_addr1     = getattr(order, "shipping_address1", None) or ""
+    in_addr2     = getattr(order, "shipping_address2", None) or ""
+
+    errors: List[str] = []
+    suggestions: List[str] = []
+
+    # 0) Easybox: considerate valide, dar cerem totuși ZIP format corect
+    if detect_easybox(in_addr1, in_addr2):
+        z6 = re.sub(r"\D", "", in_zip_raw or "").zfill(6)
+        if not re.fullmatch(r"\d{6}", z6):
+            msg = "Adresă locker detectată, dar codul poștal lipsește sau are format greșit (6 cifre)."
+            _set_order_fields(order, "invalid", 40, [msg], [])
+            return ValidationResult(False, 40, [msg], [])
+        zip_rows = await _load_by_zip(db, z6)
+        if not zip_rows:
+            msg = f"Adresă locker detectată, dar codul poștal {z6} nu există în nomenclatorul din baza de date."
+            _set_order_fields(order, "invalid", 40, [msg], [])
+            return ValidationResult(False, 40, [msg], [])
+        _set_order_fields(order, "valid", 100, ["Adresă locker (easybox/pick-up) detectată — valid."], [])
+        return ValidationResult(True, 100, ["Adresă locker (easybox/pick-up) detectată — valid."], [])
+
+    # 1) București: normalizează sectorul dacă apare
+    in_judet, in_city, detected_sector = bucharest_fix(in_judet_raw, in_city_raw, in_addr1, in_addr2)
+    if detected_sector and (norm_text(in_judet_raw) != "bucuresti" or norm_text(in_city_raw) != "bucuresti"):
+        suggestions.append(f"Recomandat: Județ='București' și Localitate='București' (sector {detected_sector}).")
+
+    # 2) Extrage stradă & număr (strict pe număr)
+    street1 = street_core(in_addr1)
+    street2 = street_core(in_addr2)
+    chosen_street = street1 if len(street1) >= len(street2) else street2
+
+    # tratează „FN / F.N. / fara nr / fără număr” ca lipsă număr
+    NO_NUM_RE = re.compile(r"\b(f\.?\s*n\.?|fara\s+nr\.?|fara\s+numar|fără\s+număr)\b", re.I)
+    if NO_NUM_RE.search(" ".join([in_addr1, in_addr2])):
+        chosen_number = None
+    else:
+        chosen_number = _has_real_house_number(in_addr1) or _has_real_house_number(in_addr2)
+
+    # 3) ZIP obligatoriu (format + existență în DB)
+    z6 = re.sub(r"\D", "", in_zip_raw or "").zfill(6)
+    if not re.fullmatch(r"\d{6}", z6):
+        errors.append("Cod poștal lipsă sau format greșit (trebuie 6 cifre).")
+
+    zip_rows = await _load_by_zip(db, z6) if not errors else []
+    if not errors and not zip_rows:
+        errors.append(f"Codul poștal {z6} nu există în nomenclatorul din baza de date.")
+
+    # 4) Numărul este obligatoriu (exceptând easybox)
+    if not chosen_number:
+        errors.append("Numărul stradal lipsește din adresă (obligatoriu).")
+
+    # 5) Validare J/L vs ZIP (ZIP-first)
+    if zip_rows:
+        j_owner, l_owner = _zip_owner_stats(zip_rows)
+        if j_owner and l_owner:
+            if not same_locality(j_owner, in_judet) or not same_locality(l_owner, in_city):
+                errors.append("Județ/localitate nu corespund codului poștal.")
+                suggestions.append(f"Actualizează la: {l_owner}, {j_owner} (conform ZIP).")
+
+    # 6) Sugestie de stradă (fuzzy în perimetrul ZIP) — nu blochează validarea
+    if zip_rows and chosen_street:
+        rows_same_loc = _rows_for_street(zip_rows, chosen_street) or zip_rows
+        # dacă nu găsim stradă apropiată, doar sugerăm verificarea denumirii
+        # (fuzzy integrat în _rows_for_street prin detect_tip_from_raw + normalizări)
+        if not rows_same_loc:
+            suggestions.append("Verifică denumirea străzii (nu găsesc potrivire pentru localitatea din ZIP).")
+
+    # 7) Emitere rezultat
+    if errors:
+        _set_order_fields(order, "invalid", 40, errors, suggestions)
+        return ValidationResult(False, 40, errors, suggestions)
+
+    _set_order_fields(order, "valid", 100, [], suggestions)
+    return ValidationResult(True, 100, [], suggestions)
*** End Patch
