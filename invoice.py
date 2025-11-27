import streamlit as st
import pandas as pd
import pdfplumber
import re
from datetime import datetime
import matplotlib.pyplot as plt

st.set_page_config(layout="wide", page_title="Dashboard Billing - Auto PDF Parser")

# ---------- helper functions ----------
months_id = {
    'januari':1,'februari':2,'maret':3,'april':4,'mei':5,'juni':6,
    'juli':7,'agustus':8,'september':9,'oktober':10,'november':11,'desember':12
}

def parse_indonesian_date(text):
    """
    Parse date strings like '26 September 2025' or '26 Sep 2025' to datetime.
    Returns pd.Timestamp or None.
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    # try common pattern dd Monthname yyyy
    m = re.search(r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', text)
    if m:
        d = int(m.group(1))
        mon = m.group(2).lower()
        year = int(m.group(3))
        # try mapping month
        mon_num = months_id.get(mon)
        if mon_num is None:
            # try abbreviated english
            try:
                return pd.to_datetime(text, dayfirst=True, errors='coerce')
            except:
                return None
        try:
            return pd.Timestamp(year, mon_num, d)
        except:
            return None
    # fallback: try pandas parser
    try:
        return pd.to_datetime(text, dayfirst=True, errors='coerce')
    except:
        return None

def extract_invoice_text_from_pdf_bytes(file_bytes):
    """Extract text from all pages using pdfplumber and return as single string."""
    text_pages = []
    try:
        with pdfplumber.open(file_bytes) as pdf:
            for page in pdf.pages:
                text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                text_pages.append(text)
    except Exception as e:
        st.warning(f"Error reading PDF: {e}")
    return "\n".join(text_pages)

def parse_invoice_fields(text):
    """
    Given the whole text of one invoice, try to extract:
    - nomor_faktur
    - tanggal_faktur
    - jatuh_tempo  (may be None)
    - klien
    - total (grand total)
    Returns dict.
    """
    res = {"Nomor Faktur": None, "Tanggal Faktur": None, "Jatuh Tempo": None, "Klien": None, "Total": None}

    # Normalize spaces
    t = re.sub(r'\r','\n', text)
    t = re.sub(r'\n\s+\n', '\n\n', t)
    lower = t.lower()

    # 1) Nomor Faktur
    m = re.search(r'nomor\s+faktur\s*[:\-]?\s*([0-9A-Za-z\-]+)', t, flags=re.IGNORECASE)
    if m:
        res["Nomor Faktur"] = m.group(1).strip()

    # 2) Tanggal Faktur
    m = re.search(r'tanggal\s+faktur\s*[:\-]?\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})', t, flags=re.IGNORECASE)
    if m:
        dt = parse_indonesian_date(m.group(1))
        res["Tanggal Faktur"] = dt

    # 3) Jatuh Tempo (optional)
    m = re.search(r'jatuh\s+tempo\s*[:\-]?\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})', t, flags=re.IGNORECASE)
    if m:
        dt = parse_indonesian_date(m.group(1))
        res["Jatuh Tempo"] = dt
    else:
        # sometimes label 'Due Date' or missing — keep None
        res["Jatuh Tempo"] = None

    # 4) Klien / Ditagihkan Kepada
    m = re.search(
        r'ditagih(?:kan)?\s+kepada\s*[:\-]?\s*(.+?)(?:\n\s*\n|no\s+dok|informasi\s+pembayaran|nomor\s+faktur)',
        t, flags=re.IGNORECASE | re.DOTALL
    )
    if m:
        block = m.group(1).strip()
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        # buang baris yang hanya angka (ID pelanggan)
        lines = [ln for ln in lines if not re.fullmatch(r'\d{5,}', ln)]
        # ambil hanya baris yang mengandung nama perusahaan
        client_lines = []
        for ln in lines:
            if re.match(r'^(jl|jalan)\b', ln, flags=re.IGNORECASE):
                break
            # hanya simpan baris yang mengandung PT atau Tbk
            if re.search(r'\bpt\b', ln, flags=re.IGNORECASE) or re.search(r'\btbk\b', ln, flags=re.IGNORECASE):
                client_lines.append(ln)
        if client_lines:
            res["Klien"] = ', '.join(client_lines)

    # fallback: cari nama perusahaan langsung
    if not res["Klien"]:
        m = re.search(r'(pt\s+[a-z0-9\.\- ,]{2,80}(?:tbk)?)', lower, flags=re.IGNORECASE)
        if m:
            res["Klien"] = m.group(0).strip().title()


    # 5) Total / Grand Total / Total: search from bottom:
    # common labels: "Grand Total", "Total", "Sub Total" -> prefer Grand Total -> Total (on right)
    m = re.search(r'grand\s+total\s*[:\-]?\s*([0-9\.,]+)', t, flags=re.IGNORECASE)
    if m:
        res["Total"] = parse_amount_str(m.group(1))
    else:
        # search 'Total' occurrences near bottom; choose last numeric after 'Total' or last big number
        candidates = re.findall(r'(?:grand total|total|sub total|sub\s+total)[^\d\n\r]*([0-9\.,]{3,})', t, flags=re.IGNORECASE)
        if candidates:
            res["Total"] = parse_amount_str(candidates[-1])
        else:
            # fallback: find the last large number in document (likely the total)
            nums = re.findall(r'([0-9]{1,3}(?:[.,][0-9]{3})+(?:[.,][0-9]{2})?)', t)
            if nums:
                res["Total"] = parse_amount_str(nums[-1])

    return res

def parse_amount_str(s):
    """Parse strings like '17.100.000' or '17,100,000.00' to float/int"""
    if s is None:
        return None
    s = s.strip()
    # remove non digits except dot and comma
    s = re.sub(r'[^\d,\.]', '', s)
    # if both comma and dot present, decide:
    if ',' in s and '.' in s:
        # assume dot thousands and comma decimals? but in Indonesian often dot thousands. We'll remove dots and commas.
        s = s.replace('.', '').replace(',', '')
    else:
        # if only dots (e.g., '17.100.000') remove dots
        if s.count('.') > 0 and s.count(',') == 0:
            s = s.replace('.', '')
        # if only commas, remove commas
        if s.count(',') > 0 and s.count('.') == 0:
            s = s.replace(',', '')
    try:
        return float(s)
    except:
        return None

# ---------- Streamlit app ----------
st.title("Dashboard Billing — Auto PDF Invoice Parser")
st.markdown("Upload banyak file PDF invoice (template sama). Sistem akan otomatis ekstrak Nomor Faktur, Tanggal Faktur, Jatuh Tempo (jika ada), Klien, dan Total.\n\n*Catatan:* setelah upload, cek tabel hasil parsing. Kamu dapat memperbaiki sel yang salah sebelum grafik diupdate.")

# Session state for invoices
if "invoices_df" not in st.session_state:
    st.session_state.invoices_df = pd.DataFrame(columns=["Nomor Faktur","Tanggal Faktur","Jatuh Tempo","Klien","Total","Sumber File"])

# upload area
uploaded = st.file_uploader("Upload file PDF (boleh banyak) — drag & drop or pilih file", type=["pdf"], accept_multiple_files=True)

if uploaded:
    progress = st.progress(0)
    total_files = len(uploaded)
    i = 0
    parse_logs = []
    for f in uploaded:
        i += 1
        progress.progress(int(i/total_files*100))
        try:
            raw_text = extract_invoice_text_from_pdf_bytes(f)
            parsed = parse_invoice_fields(raw_text)
            parsed_row = {
                "Nomor Faktur": parsed.get("Nomor Faktur") or "",
                "Tanggal Faktur": parsed.get("Tanggal Faktur"),
                "Jatuh Tempo": parsed.get("Jatuh Tempo"),
                "Klien": parsed.get("Klien") or "",
                "Total": parsed.get("Total") or 0.0,
                "Sumber File": getattr(f, "name", "uploaded.pdf")
            }
            # If tanggal is Timestamp, convert to date
            if isinstance(parsed_row["Tanggal Faktur"], pd.Timestamp):
                parsed_row["Tanggal Faktur"] = parsed_row["Tanggal Faktur"].date()
            if isinstance(parsed_row["Jatuh Tempo"], pd.Timestamp):
                parsed_row["Jatuh Tempo"] = parsed_row["Jatuh Tempo"].date()
            st.session_state.invoices_df = pd.concat([st.session_state.invoices_df, pd.DataFrame([parsed_row])], ignore_index=True)
            parse_logs.append(f"{f.name}: parsed OK")
        except Exception as e:
            parse_logs.append(f"{f.name}: ERROR {e}")
    progress.empty()
    st.success("Upload & parsing selesai.")
    st.write("Log parsing:")
    st.write("\n".join(parse_logs))

# show current dataframe with editor for corrections
st.subheader("Tabel Hasil Parsing (boleh diedit manual jika perlu)")
if not st.session_state.invoices_df.empty:
    # show editable table
    edited = st.data_editor(st.session_state.invoices_df, num_rows="dynamic")
    # update session with edits
    st.session_state.invoices_df = edited

    # normalize dates
    df = st.session_state.invoices_df.copy()
    # ensure Tanggal Faktur is datetime
    df["Tanggal Faktur"] = pd.to_datetime(df["Tanggal Faktur"], errors="coerce")
    df["Total"] = pd.to_numeric(df["Total"], errors="coerce").fillna(0.0)

    # ---------- Charts ----------
    st.subheader("Grafik")

    # Line chart: tren penagihan per klien (date index)
    st.markdown("*Tren Penagihan per Klien (Line Chart)*")
    if not df["Tanggal Faktur"].isna().all():
        trend = df.groupby(["Tanggal Faktur","Klien"])["Total"].sum().unstack(fill_value=0)
        st.line_chart(trend)
    else:
        st.info("Belum ada tanggal valid untuk line chart.")

    # Bar chart: total per bulan
    st.markdown("*Total Penagihan per Bulan (Bar Chart)*")
    df["Bulan"] = df["Tanggal Faktur"].dt.to_period("M").astype(str)
    monthly = df.groupby("Bulan")["Total"].sum().sort_index()
    if not monthly.empty:
        st.bar_chart(monthly)
    else:
        st.info("Belum ada data bulanan.")

    # Pie: kontribusi per klien
    st.markdown("*Kontribusi Pelanggan (Pie Chart)*")
    client_sum = df.groupby("Klien")["Total"].sum().sort_values(ascending=False)
    if not client_sum.empty:
        fig, ax = plt.subplots(figsize=(6,6))
        ax.pie(client_sum, labels=client_sum.index, autopct="%1.1f%%", startangle=90)
        ax.axis("equal")
        st.pyplot(fig)
    else:
        st.info("Belum ada data untuk pie chart.")

else:
    st.info("Belum ada invoice ter-upload. Upload file PDF untuk memulai.")