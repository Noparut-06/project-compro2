import csv
import os
import struct
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime

# ==========================================
# CONSTANTS & CONFIGURATIONS
# ==========================================
# ขนาด fixed-length สำหรับฟิลด์
# Struct Format: < (Little-Endian)
# i  : comp_id (int4)
# 16s: pc_name (char[16])
# 16s: zone (char[16])
# i  : ram_gb (int4)
# f  : rate_per_hr (float4)
# 16s: status (char[16])
# ?  : is_active (bool1)
RECORD_FORMAT = "<i16s16sif16s?"
RECORD_SIZE = struct.calcsize(RECORD_FORMAT)
LEGACY_MEMBER_APPLICATION_FORMAT = "<i32s16s20s16s"
LEGACY_MEMBER_APPLICATION_SIZE = struct.calcsize(LEGACY_MEMBER_APPLICATION_FORMAT)
MEMBER_APPLICATION_FORMAT = "<i32s16s24s20s?"
MEMBER_APPLICATION_SIZE = struct.calcsize(MEMBER_APPLICATION_FORMAT)
LEGACY_BOOKING_FORMAT = "<iiif20s?"
LEGACY_BOOKING_SIZE = struct.calcsize(LEGACY_BOOKING_FORMAT)
PREVIOUS_BOOKING_FORMAT = "<iiiiff24s20s?"
PREVIOUS_BOOKING_SIZE = struct.calcsize(PREVIOUS_BOOKING_FORMAT)
BOOKING_FORMAT = "<iiiiff24s20s10s24s?"
BOOKING_SIZE = struct.calcsize(BOOKING_FORMAT)
RATE_PER_HOUR = 20.0
BOOKING_SLOTS = {
    "1": ("13:00-16:00", 3),
    "2": ("16:00-20:00", 4),
}
VIP_DISCOUNTS = {
    "VIP ระดับ 1": 10.0,
    "VIP ระดับ 2": 13.0,
    "VIP ระดับ 3": 20.0,
}

# กำหนดไฟล์ไบนารี 3 ไฟล์ และไฟล์รายงาน 1 ไฟล์
FILE_COMPUTERS = "computers.bin"
FILE_CUSTOMERS = "customers.bin"
FILE_BOOKINGS = "bookings.bin"
FILE_REPORT = "summary_report.txt"
FILE_MEMBER_REPORT = "member_applicants_report.txt"
FILE_ISSUES = "problem_reports.csv"
FILE_ISSUE_REPORT = "problem_reports.txt"
ISSUE_FIELDS = (
    "report_id", "comp_id", "pc_name", "issue_date", "issue_type",
    "description", "reported_at", "status",
)
CUSTOM_ISSUE = "อื่นๆ"
ISSUE_TYPES = {
    "1": "RAM พัง",
    "2": "จอเป็นเส้น",
    "3": "จอพัง",
    "4": CUSTOM_ISSUE,  # ลูกค้ากรอกเอง
}

# หน่วยความจำชั่วคราวสำหรับเก็บประวัติการทำงานล่าสุดในเซสชัน (Audit Log)
ACTION_LOGS = []

# ==========================================
# TABLE HELPERS (จัดตารางให้ตรงแม้มีภาษาไทย)
# ==========================================
def display_width(text) -> int:
    """ความกว้างที่แสดงจริงบน terminal: สระบน/ล่างและวรรณยุกต์ไทยกว้าง 0 ช่อง"""
    width = 0
    for ch in str(text):
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text, width: int) -> str:
    """เติมช่องว่างท้ายข้อความตามความกว้างที่แสดงจริง (แทน f"{x:<N}")"""
    text = str(text)
    return text + " " * max(0, width - display_width(text))


def clip(text, width: int) -> str:
    """ตัดข้อความให้ไม่เกินความกว้างที่แสดงจริง"""
    out, used = "", 0
    for ch in str(text):
        w = display_width(ch)
        if used + w > width:
            break
        out += ch
        used += w
    return out


def format_table(headers, rows, min_widths=None, row_separators=False) -> list[str]:
    """สร้างตารางข้อความ โดยคำนวณความกว้างคอลัมน์จากข้อมูลจริง เส้นจึงตรงเสมอ"""
    rows = [[str(cell) for cell in row] for row in rows]
    min_widths = min_widths or [0] * len(headers)
    widths = [
        max([min_widths[i], display_width(header)] + [display_width(row[i]) for row in rows])
        for i, header in enumerate(headers)
    ]
    border = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def fmt(cells):
        return "| " + " | ".join(pad(cell, w) for cell, w in zip(cells, widths)) + " |"

    lines = [border, fmt(headers), border]
    for row in rows:
        lines.append(fmt(row))
        if row_separators:
            lines.append(border)
    return lines


def log_action(action: str):
    """บันทึกประวัติการทำงานพร้อมประทับเวลา"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ACTION_LOGS.append(f"[{now_str}] {action}")


def normalize_member_level(member_level: str) -> str:
    normalized_level = member_level.strip().casefold()
    level_aliases = {
        "vip": "VIP ระดับ 1",
        "vip1": "VIP ระดับ 1",
        "vip2": "VIP ระดับ 2",
        "vip3": "VIP ระดับ 3",
    }
    if normalized_level in level_aliases:
        return level_aliases[normalized_level]
    for level in VIP_DISCOUNTS:
        if normalized_level == level.casefold():
            return level
    if normalized_level == "ทั่วไป":
        return "ทั่วไป"
    return "ทั่วไป"


def member_discount(member_level: str) -> float:
    return VIP_DISCOUNTS.get(normalize_member_level(member_level), 0.0)


def normalize_computer_zone(zone: str) -> str:
    zone_aliases = {
        "standard": "Standard",
        "vip": "VIP",
        "vip1": "VIP1",
        "vip2": "VIP2",
        "vip3": "VIP3",
    }
    return zone_aliases.get(zone.strip().casefold(), zone.strip())


# ==========================================
# BINARY I/O FUNCTIONS (COMPUTER RECORD)
# ==========================================
def pack_computer_record(comp_id: int, pc_name: str, zone: str, ram_gb: int, 
                         rate_per_hr: float, status: str, is_active: bool) -> bytes:
    """แปลงข้อมูลคอมพิวเตอร์เป็น bytes ขนาด fixed-length ด้วย struct.pack"""
    return struct.pack(
        RECORD_FORMAT,
        comp_id,
        pc_name.encode('utf-8')[:16].ljust(16, b'\x00'),
        zone.encode('utf-8')[:16].ljust(16, b'\x00'),
        ram_gb,
        rate_per_hr,
        status.encode('utf-8')[:16].ljust(16, b'\x00'),
        is_active
    )


def unpack_computer_record(buffer: bytes) -> dict:
    """แปลง bytes กลับมาเป็น dictionary ข้อมูลคอมพิวเตอร์"""
    unpacked = struct.unpack(RECORD_FORMAT, buffer)
    return {
        "comp_id": unpacked[0],
        "pc_name": unpacked[1].decode('utf-8').rstrip('\x00'),
        "zone": normalize_computer_zone(unpacked[2].decode('utf-8').rstrip('\x00')),
        "ram_gb": unpacked[3],
        "rate_per_hr": unpacked[4],
        "status": unpacked[5].decode('utf-8').rstrip('\x00'),
        "is_active": unpacked[6]
    }


def init_data_files():
    """สร้างไฟล์ข้อมูลเปล่าเมื่อยังไม่มีไฟล์ โดยไม่ใส่ข้อมูลตัวอย่าง"""
    for file_path in (FILE_COMPUTERS, FILE_CUSTOMERS, FILE_BOOKINGS):
        if not os.path.exists(file_path):
            with open(file_path, "wb") as file:
                file.flush()
                os.fsync(file.fileno())


def read_all_computers() -> list[dict]:
    """อ่านระเบียนคอมพิวเตอร์ทั้งหมดจากไฟล์ไบนารี"""
    records = []
    if not os.path.exists(FILE_COMPUTERS):
        return records

    with open(FILE_COMPUTERS, "rb") as f:
        while chunk := f.read(RECORD_SIZE):
            if len(chunk) == RECORD_SIZE:
                records.append(unpack_computer_record(chunk))
    return records


def pack_booking(booking_id: int, comp_id: int, hours: int, booked_at: str,
                 is_active: bool = True, user_id: int = 0,
                 member_level: str = "ทั่วไป", discount_percent: float = 0.0,
                 booking_date: str = "", time_slot: str = "") -> bytes:
    level_bytes = normalize_member_level(member_level).encode("utf-8")
    date_bytes = (booking_date or booked_at[:10]).encode("ascii")
    time_slot_bytes = time_slot.encode("ascii")
    if len(level_bytes) > 24 or len(date_bytes) > 10 or len(time_slot_bytes) > 24:
        raise ValueError("Booking field exceeds its fixed binary length")
    return struct.pack(
        BOOKING_FORMAT,
        booking_id,
        comp_id,
        user_id,
        hours,
        RATE_PER_HOUR,
        discount_percent,
        level_bytes.ljust(24, b"\x00"),
        booked_at.encode("ascii").ljust(20, b"\x00"),
        date_bytes.ljust(10, b"\x00"),
        time_slot_bytes.ljust(24, b"\x00"),
        is_active,
    )


def unpack_booking(buffer: bytes) -> dict:
    (booking_id, comp_id, user_id, hours, rate_per_hour, discount_percent,
     member_level, booked_at, booking_date, time_slot, is_active) = struct.unpack(
        BOOKING_FORMAT, buffer
    )
    subtotal = hours * rate_per_hour
    return {
        "booking_id": booking_id,
        "comp_id": comp_id,
        "user_id": user_id,
        "hours": hours,
        "rate_per_hour": rate_per_hour,
        "member_level": normalize_member_level(member_level.decode("utf-8").rstrip("\x00")),
        "discount_percent": discount_percent,
        "subtotal": subtotal,
        "total": subtotal * (1 - discount_percent / 100),
        "booked_at": booked_at.decode("ascii").rstrip("\x00"),
        "booking_date": booking_date.decode("ascii").rstrip("\x00"),
        "time_slot": time_slot.decode("ascii").rstrip("\x00"),
        "is_active": is_active,
    }


def read_all_bookings() -> list[dict]:
    bookings = []
    if not os.path.exists(FILE_BOOKINGS):
        return bookings

    file_size = os.path.getsize(FILE_BOOKINGS)
    if file_size == 0:
        return bookings
    if file_size % BOOKING_SIZE == 0:
        record_format, record_size = "current", BOOKING_SIZE
    elif file_size % PREVIOUS_BOOKING_SIZE == 0:
        record_format, record_size = "previous", PREVIOUS_BOOKING_SIZE
    elif file_size % LEGACY_BOOKING_SIZE == 0:
        record_format, record_size = "legacy", LEGACY_BOOKING_SIZE
    else:
        raise ValueError("bookings.bin has an incomplete or unknown record format")

    with open(FILE_BOOKINGS, "rb") as file:
        while chunk := file.read(record_size):
            if len(chunk) != record_size:
                break
            if record_format == "current":
                bookings.append(unpack_booking(chunk))
                continue

            if record_format == "previous":
                (booking_id, comp_id, user_id, hours, rate_per_hour,
                 discount_percent, member_level, booked_at, is_active) = struct.unpack(
                    PREVIOUS_BOOKING_FORMAT, chunk
                )
                member_level = normalize_member_level(
                    member_level.decode("utf-8").rstrip("\x00")
                )
                subtotal = hours * rate_per_hour
                bookings.append(
                    {
                        "booking_id": booking_id,
                        "comp_id": comp_id,
                        "user_id": user_id,
                        "hours": hours,
                        "rate_per_hour": rate_per_hour,
                        "member_level": member_level,
                        "discount_percent": discount_percent,
                        "subtotal": subtotal,
                        "total": subtotal * (1 - discount_percent / 100),
                        "booked_at": booked_at.decode("ascii").rstrip("\x00"),
                        "booking_date": booked_at.decode("ascii").rstrip("\x00")[:10],
                        "time_slot": "",
                        "is_active": is_active,
                    }
                )
                continue

            booking_id, comp_id, hours, rate_per_hour, booked_at, is_active = struct.unpack(
                LEGACY_BOOKING_FORMAT, chunk
            )
            subtotal = hours * rate_per_hour
            bookings.append(
                {
                    "booking_id": booking_id,
                    "comp_id": comp_id,
                    "user_id": 0,
                    "hours": hours,
                    "rate_per_hour": rate_per_hour,
                    "member_level": "ทั่วไป",
                    "discount_percent": 0.0,
                    "subtotal": subtotal,
                    "total": subtotal,
                    "booked_at": booked_at.decode("ascii").rstrip("\x00"),
                    "booking_date": booked_at.decode("ascii").rstrip("\x00")[:10],
                    "time_slot": "",
                    "is_active": is_active,
                }
            )
    return bookings


def write_all_bookings(bookings: list[dict]) -> None:
    with open(FILE_BOOKINGS, "wb") as file:
        for booking in bookings:
            file.write(
                pack_booking(
                    booking["booking_id"],
                    booking["comp_id"],
                    booking["hours"],
                    booking["booked_at"],
                    booking["is_active"],
                    booking.get("user_id", 0),
                    booking.get("member_level", "ทั่วไป"),
                    booking.get("discount_percent", 0.0),
                    booking.get("booking_date", ""),
                    booking.get("time_slot", ""),
                )
            )
        file.flush()
        os.fsync(file.fileno())


def pack_member_application(user_id: int, name: str, member_level: str,
                            applied_at: str, is_active: bool = True,
                            legacy_phone: str = "") -> bytes:
    name_bytes = name.encode("utf-8")
    phone_bytes = legacy_phone.encode("utf-8")
    level_bytes = member_level.encode("utf-8")
    if len(name_bytes) > 32 or len(phone_bytes) > 16 or len(level_bytes) > 24:
        raise ValueError("Member field exceeds its fixed binary length")
    return struct.pack(
        MEMBER_APPLICATION_FORMAT,
        user_id,
        name_bytes.ljust(32, b"\x00"),
        phone_bytes.ljust(16, b"\x00"),
        level_bytes.ljust(24, b"\x00"),
        applied_at.encode("ascii").ljust(20, b"\x00"),
        is_active,
    )


def unpack_member_application(buffer: bytes) -> dict:
    user_id, name, legacy_phone, member_level, applied_at, is_active = struct.unpack(
        MEMBER_APPLICATION_FORMAT, buffer
    )
    return {
        "user_id": user_id,
        "name": name.decode("utf-8").rstrip("\x00"),
        "legacy_phone": legacy_phone.decode("utf-8").rstrip("\x00"),
        "member_level": normalize_member_level(member_level.decode("utf-8").rstrip("\x00")),
        "applied_at": applied_at.decode("ascii").rstrip("\x00"),
        "is_active": is_active,
    }


def read_member_applications() -> list[dict]:
    applications = []
    if not os.path.exists(FILE_CUSTOMERS):
        return applications

    file_size = os.path.getsize(FILE_CUSTOMERS)
    is_new_format = file_size == 0 or (
        file_size % MEMBER_APPLICATION_SIZE == 0
        and file_size % LEGACY_MEMBER_APPLICATION_SIZE != 0
    )
    with open(FILE_CUSTOMERS, "rb") as file:
        record_size = MEMBER_APPLICATION_SIZE if is_new_format else LEGACY_MEMBER_APPLICATION_SIZE
        while chunk := file.read(record_size):
            if len(chunk) != record_size:
                break
            if is_new_format:
                applications.append(unpack_member_application(chunk))
                continue

            user_id, name, phone, applied_at, legacy_status = struct.unpack(
                LEGACY_MEMBER_APPLICATION_FORMAT, chunk
            )
            legacy_status = legacy_status.decode("utf-8").rstrip("\x00")
            member_level = normalize_member_level(legacy_status)
            applications.append(
                {
                    "user_id": user_id,
                    "name": name.decode("utf-8").rstrip("\x00"),
                    "legacy_phone": phone.decode("utf-8").rstrip("\x00"),
                    "member_level": member_level,
                    "applied_at": applied_at.decode("ascii").rstrip("\x00"),
                    "is_active": legacy_status != "Deleted",
                }
            )
    return applications


# ==========================================
# INPUT VALIDATION HELPERS
# ==========================================
def input_int(prompt: str, min_val: int = None, max_val: int = None) -> int:
    """ตรวจสอบอินพุตตัวเลขจำนวนเต็ม"""
    while True:
        val_str = input(prompt).strip()
        try:
            val = int(val_str)
            if min_val is not None and val < min_val:
                print(f"[!] ค่าต้องไม่น้อยกว่า {min_val}")
                continue
            if max_val is not None and val > max_val:
                print(f"[!] ค่าต้องไม่เกิน {max_val}")
                continue
            return val
        except ValueError:
            print("[!] อินพุตไม่ถูกต้อง: กรุณากรอกเป็นตัวเลขจำนวนเต็มเท่านั้น")


def input_float(prompt: str, min_val: float = None) -> float:
    """ตรวจสอบอินพุตตัวเลขทศนิยม"""
    while True:
        val_str = input(prompt).strip()
        try:
            val = float(val_str)
            if min_val is not None and val < min_val:
                print(f"[!] ค่าต้องไม่น้อยกว่า {min_val}")
                continue
            return val
        except ValueError:
            print("[!] อินพุตไม่ถูกต้อง: กรุณากรอกเป็นตัวเลขทศนิยมเท่านั้น")


def input_string(prompt: str, max_length: int = 15, choices: list = None) -> str:
    """ตรวจสอบอินพุตข้อความ พร้อมจำกัดความยาวและตัวเลือก"""
    while True:
        val = input(prompt).strip()
        if not val:
            print("[!] ข้อความต้องไม่เป็นค่าว่าง")
            continue
        if len(val.encode('utf-8')) > max_length:
            print(f"[!] ความยาวข้อความเกิน {max_length} ไบต์ (กรุณากรอกใหม่ให้สั้นลง)")
            continue
        if choices and val.lower() not in [c.lower() for c in choices]:
            print(f"[!] ค่าที่กรอกต้องเป็นหนึ่งในนี้เท่านั้น: {', '.join(choices)}")
            continue
        return val


# ==========================================
# REPORT GENERATION FUNCTION
# ==========================================
def generate_report(output_file: str | None = None) -> str:
    """สร้างรายงานข้อความ (.txt) พร้อมข้อมูลสรุป ช่องว่าง และประวัติการทำงาน"""
    if output_file is None:
        output_file = FILE_REPORT
    records = read_all_computers()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S (+07:00)")

    lines = []
    lines.append("Cyber Cafe Booking System - Computer Report")
    lines.append(f"Generated At : {now_str}")
    lines.append("App Version  : 1.0")
    lines.append("Endianness   : Little-Endian")
    lines.append("Encoding     : UTF-8 (fixed-length)")
    lines.append("")

    total_records = len(records)
    active_recs = [r for r in records if r["is_active"]]
    deleted_count = total_records - len(active_recs)

    in_use_count = sum(1 for r in active_recs if r["status"] == "In Use")
    available_count = sum(1 for r in active_recs if r["status"] == "Available")
    maint_count = sum(1 for r in active_recs if r["status"] == "Maint")

    rates = [r["rate_per_hr"] for r in active_recs]
    min_rate = min(rates) if rates else 0.0
    max_rate = max(rates) if rates else 0.0
    avg_rate = sum(rates) / len(rates) if rates else 0.0

    zone_counts = {}
    for r in active_recs:
        z = r["zone"]
        zone_counts[z] = zone_counts.get(z, 0) + 1

    # สร้างแถวข้อมูลตาราง
    table_rows = []
    for r in records:
        duration_hours = r["rate_per_hr"] / RATE_PER_HOUR if RATE_PER_HOUR else 0
        table_rows.append([
            r["comp_id"], r["pc_name"], r["zone"], r["ram_gb"],
            f"{r['rate_per_hr']:.2f} /{duration_hours:g} Hour",
            r["status"], "Yes" if r["is_active"] else "No",
        ])
    lines.extend(format_table(
        ["CompID", "PC Name", "Zone", "RAM (GB)", "Rate (THB / Duration)", "Status", "Active"],
        table_rows, min_widths=[6, 10, 10, 8, 26, 8, 6], row_separators=True,
    ))
    bookings = [booking for booking in read_all_bookings() if booking["is_active"]]
    lines.extend(["", "Computer Bookings (Rate: 20 THB per hour)"])
    lines.extend(format_table(
        ["BookingID", "CompID", "UserID", "Member Level", "Hours", "Rate (THB/Hour)",
         "Discount", "Total (THB)", "Booked At"],
        [
            [b["booking_id"], b["comp_id"], b["user_id"], b["member_level"], b["hours"],
             f"{b['rate_per_hour']:.2f}", f"{b['discount_percent']:.1f}%",
             f"{b['total']:.2f}", b["booked_at"]]
            for b in bookings
        ],
        min_widths=[10, 6, 8, 16, 5, 16, 8, 12, 19],
    ))

    lines.append("")
    lines.append("Summary (นับเฉพาะเครื่องสถานะ Active)")
    lines.append(f"- Total Computers (records) : {total_records}")
    lines.append(f"- Active Computers          : {len(active_recs)}")
    lines.append(f"- Deleted Computers         : {deleted_count}")
    lines.append(f"- Free Slots (Unused/Soft-Deleted) : {deleted_count}")
    lines.append(f"- Currently In Use          : {in_use_count}")
    lines.append(f"- Available Now             : {available_count}")
    lines.append(f"- Under Maintenance         : {maint_count}")
    lines.append(f"- Active Bookings            : {len(bookings)}")
    lines.append(f"- Total Booked Hours         : {sum(booking['hours'] for booking in bookings)}")
    lines.append(f"- Booking Revenue (THB)      : {sum(booking['total'] for booking in bookings):.2f}")
    lines.append(f"- VIP Discounts (THB)        : {sum(booking['subtotal'] - booking['total'] for booking in bookings):.2f}")
    lines.append("")
    lines.append("Package Price Statistics (THB, Active only)")
    lines.append(f"- Min : {min_rate:.2f}")
    lines.append(f"- Max : {max_rate:.2f}")
    lines.append(f"- Avg : {avg_rate:.2f}")
    lines.append("")
    lines.append("Computers by Zone (Active only)")
    for z in sorted(zone_counts.keys()):
        lines.append(f"- {z:<10} : {zone_counts[z]}")

    # ประวัติการทำงานล่าสุด (Audit Logs)
    lines.append("")
    lines.append("Recent Activity Logs")
    if ACTION_LOGS:
        for log in ACTION_LOGS[-10:]:  # แสดงสูงสุด 10 รายการล่าสุด
            lines.append(f"- {log}")
    else:
        lines.append("- No activity recorded in current session.")

    report_content = "\n".join(lines)

    # เขียนลงไฟล์ .txt พร้อม flush & sync เพื่อความปลอดภัย
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(report_content)
        f.flush()
        os.fsync(f.fileno())

    return report_content


def _read_problem_reports() -> list[dict[str, str]]:
    if not os.path.exists(FILE_ISSUES):
        return []
    with open(FILE_ISSUES, "r", newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def _write_problem_reports(reports: list[dict[str, str]]) -> None:
    with open(FILE_ISSUES, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=ISSUE_FIELDS)
        writer.writeheader()
        writer.writerows(reports)
        file.flush()
        os.fsync(file.fileno())


def _issue_date(report: dict) -> str:
    """วันที่เกิดปัญหา (ข้อมูลเก่าที่ไม่มี issue_date ให้ใช้วันที่แจ้งแทน)"""
    return report.get("issue_date") or report.get("reported_at", "")[:10]


def _issue_type(report: dict) -> str:
    return report.get("issue_type") or CUSTOM_ISSUE


def generate_issue_report(reports: list[dict[str, str]] | None = None) -> str:
    if reports is None:
        reports = _read_problem_reports()

    lines = [
        "Cyber Cafe Booking System - Problem Report",
        f"Generated At : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "Encoding     : UTF-8",
        "",
    ]
    lines.extend(format_table(
        ["ReportID", "CompID", "PC Name", "Date", "Problem", "Status"],
        [
            [r["report_id"], r["comp_id"], clip(r["pc_name"], 10), _issue_date(r),
             clip(r["description"], 24), r["status"]]
            for r in reports
        ],
        min_widths=[8, 6, 10, 10, 24, 8],
    ))

    # ---- สรุปรายเดือน: เครื่องไหนพังกี่ครั้ง / พังเพราะอะไรมากสุด ----
    valid = [item for item in reports if item["status"] != "Deleted"]
    by_month = defaultdict(lambda: defaultdict(list))
    for item in valid:
        month = _issue_date(item)[:7]
        by_month[month][(item["comp_id"], item["pc_name"])].append(item)

    lines.extend(["", "Monthly Breakdown"])
    if not by_month:
        lines.append("- ยังไม่มีรายงานปัญหา")
    for month in sorted(by_month):
        pcs = by_month[month]
        month_total = sum(len(items) for items in pcs.values())
        month_types = Counter(_issue_type(i) for items in pcs.values() for i in items)
        top_type, top_count = month_types.most_common(1)[0]
        lines.extend(["", f"[{month}] พังรวม {month_total} ครั้ง | ปัญหาที่พบบ่อยสุด: {top_type} ({top_count} ครั้ง)"])
        for (comp_id, pc_name), items in sorted(pcs.items(), key=lambda kv: -len(kv[1])):
            types = Counter(_issue_type(i) for i in items)
            top_type, top_count = types.most_common(1)[0]
            detail = ", ".join(f"{name} {count}" for name, count in types.most_common())
            lines.append(
                f"  CompID {comp_id} ({pc_name}): พัง {len(items)} ครั้ง "
                f"| พังบ่อยสุด: {top_type} ({top_count} ครั้ง)"
            )
            lines.append(f"      แยกตามปัญหา: {detail}")

    overall = Counter(_issue_type(i) for i in valid)
    lines.extend(
        [
            "",
            "Summary",
            f"- Total Problem Reports : {len(valid)}",
            f"- Open                 : {sum(item['status'] == 'Open' for item in valid)}",
            f"- Closed               : {sum(item['status'] == 'Closed' for item in valid)}",
            "- Top Issue (all time)  : "
            + (f"{overall.most_common(1)[0][0]} ({overall.most_common(1)[0][1]} ครั้ง)" if overall else "-"),
        ]
    )

    content = "\n".join(lines)
    with open(FILE_ISSUE_REPORT, "w", encoding="utf-8") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    return content


def _display_problem_reports(reports: list[dict[str, str]]) -> None:
    print(generate_issue_report(reports))


def _display_computer_status_table(computers: list[dict]) -> None:
    print("\nสถานะการใช้งานเครื่อง:")
    print("\n".join(format_table(
        ["CompID", "PC Name", "Zone", "สถานะการใช้งาน"],
        [
            [c["comp_id"], c["pc_name"], c["zone"], c["status"]]
            for c in sorted(computers, key=lambda item: item["comp_id"])
        ],
        min_widths=[8, 16, 12, 16],
    )))


def _ask_issue_date() -> str | None:
    text = input("วันที่พบปัญหา (YYYY-MM-DD, Enter = วันนี้): ").strip()
    if not text:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        print("[!] รูปแบบวันที่ไม่ถูกต้อง กรุณาใช้ YYYY-MM-DD")
        return None


def _ask_issue_type() -> tuple[str, str]:
    """คืนค่า (issue_type, description) จาก preset หรือให้ลูกค้ากรอกเอง"""
    print("ประเภทปัญหา:")
    for key, name in ISSUE_TYPES.items():
        label = f"{name} (กรอกเอง)" if name == CUSTOM_ISSUE else name
        print(f"{key}) {label}")
    choice = input_string(
        f"เลือกประเภทปัญหา (1-{len(ISSUE_TYPES)}): ", max_length=1, choices=list(ISSUE_TYPES)
    )
    issue_type = ISSUE_TYPES[choice]
    if issue_type == CUSTOM_ISSUE:
        description = input_string("กรอกปัญหาที่พบ: ", max_length=200)
    else:
        description = issue_type
    return issue_type, description


def _add_problem_report(computers: list[dict]) -> None:
    """จำลองลูกค้ารายงานปัญหา: วันที่ -> เครื่อง -> ประเภทปัญหา"""
    active_computers = [computer for computer in computers if computer["is_active"]]
    if not active_computers:
        print("[!] ยังไม่มีเครื่องในระบบ จึงรายงานปัญหาไม่ได้")
        return

    issue_date = _ask_issue_date()
    if issue_date is None:
        return

    _display_computer_status_table(active_computers)
    comp_id = input_int("CompID ที่พบปัญหา: ", min_val=1)
    computer = next((item for item in active_computers if item["comp_id"] == comp_id), None)
    if computer is None:
        print("[!] ไม่พบ CompID ที่ใช้งานอยู่")
        return

    issue_type, description = _ask_issue_type()

    reports = _read_problem_reports()
    report_id = max((int(item["report_id"]) for item in reports), default=0) + 1
    reports.append(
        {
            "report_id": str(report_id),
            "comp_id": str(comp_id),
            "pc_name": computer["pc_name"],
            "issue_date": issue_date,
            "issue_type": issue_type,
            "description": description,
            "reported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "Open",
        }
    )
    _write_problem_reports(reports)
    generate_issue_report(reports)
    log_action(f"Problem report #{report_id}: CompID {comp_id} {issue_type} on {issue_date}")
    print(f"[✓] บันทึกรายงานปัญหา #{report_id} แล้ว ({issue_date} | {computer['pc_name']} | {description})")


def _update_problem_report() -> None:
    reports = _read_problem_reports()
    report_id = input_int("หมายเลขรายงานที่ต้องการแก้ไข: ", min_val=1)
    report = next(
        (item for item in reports if int(item["report_id"]) == report_id and item["status"] != "Deleted"),
        None,
    )
    if report is None:
        print("[!] ไม่พบรายงาน หรือรายงานถูกลบแล้ว")
        return

    print(
        f"ปัจจุบัน: {_issue_date(report)} | {report['pc_name']} | "
        f"{_issue_type(report)} | {report['description']} | สถานะ: {report['status']}"
    )
    if input("แก้ไขประเภทปัญหา? (y = แก้, Enter = คงเดิม): ").strip().lower() == "y":
        report["issue_type"], report["description"] = _ask_issue_type()

    status = input("สถานะใหม่ (Open/Closed, Enter เพื่อคงเดิม): ").strip().title()
    if status and status not in ("Open", "Closed"):
        print("[!] สถานะไม่ถูกต้อง จึงคงสถานะเดิมไว้")
        status = ""
    if status:
        report["status"] = status

    _write_problem_reports(reports)
    generate_issue_report(reports)
    log_action(f"Updated problem report ID: {report_id}")
    print("[✓] แก้ไขรายงานปัญหาแล้ว")


def _delete_problem_report() -> None:
    reports = _read_problem_reports()
    report_id = input_int("หมายเลขรายงานที่ต้องการลบ: ", min_val=1)
    remaining_reports = [item for item in reports if int(item["report_id"]) != report_id]
    if len(remaining_reports) == len(reports):
        print("[!] ไม่พบหมายเลขรายงานนี้")
        return

    _write_problem_reports(remaining_reports)
    generate_issue_report(remaining_reports)
    log_action(f"Deleted problem report ID: {report_id}")
    print("[✓] ลบรายงานปัญหาและข้อมูลที่เกี่ยวข้องแล้ว")


def issue_management_menu(read_computers) -> None:
    while True:
        print("\n--- จำลองลูกค้ารายงานปัญหา ---")
        print("1) Add (ลูกค้าแจ้งปัญหา)")
        print("2) Update (แก้ไข)")
        print("3) Delete (ลบ)")
        print("4) View (ดู)")
        print("5) Generate Report (.txt)")
        print("0) ย้อนกลับหน้าหลัก")
        choice = input("เลือกเมนู (0-5): ").strip()

        if choice == "1":
            _add_problem_report(read_computers())
        elif choice == "2":
            _update_problem_report()
        elif choice == "3":
            _delete_problem_report()
        elif choice == "4":
            _display_problem_reports(_read_problem_reports())
        elif choice == "5":
            print(generate_issue_report())
        elif choice == "0":
            break
        else:
            print("[!] ตัวเลือกไม่ถูกต้อง กรุณาเลือก 0 ถึง 5")


def generate_member_report(applications: list[dict] | None = None) -> str:
    if applications is None:
        applications = read_member_applications()

    active_applications = [item for item in applications if item["is_active"]]
    deleted_count = len(applications) - len(active_applications)
    level_order = {
        "ทั่วไป": 0,
        "VIP ระดับ 1": 1,
        "VIP ระดับ 2": 2,
        "VIP ระดับ 3": 3,
    }
    active_applications.sort(
        key=lambda item: (
            level_order.get(normalize_member_level(item["member_level"]), 4),
            item["user_id"],
        )
    )
    category_labels = {
        "ทั่วไป": "ทั่วไป (0%)",
        **{
            level: f"{level} ({discount:g}%)"
            for level, discount in VIP_DISCOUNTS.items()
        },
    }
    lines = [
        "Cyber Cafe Booking System - Member Report",
        f"Generated At : {datetime.now().strftime('%Y-%m-%d %H:%M:%S (+07:00)')}",
        "Endianness   : Little-Endian",
        "Encoding     : UTF-8 (fixed-length)",
        "",
    ]
    lines.extend(format_table(
        ["ID USER", "ชื่อ-สกุล", "ระดับสมาชิก", "สมัครวันที่"],
        [
            [a["user_id"], a["name"],
             category_labels[normalize_member_level(a["member_level"])], a["applied_at"]]
            for a in active_applications
        ],
        min_widths=[12, 32, 22, 19],
    ))

    lines.extend(
        [
            "",
            "Summary",
            f"- Total Applications : {len(applications)}",
            f"- Active Members     : {len(active_applications)}",
            f"- Deleted Members    : {deleted_count}",
            f"- General (0%)      : {sum(normalize_member_level(item['member_level']) == 'ทั่วไป' for item in active_applications)}",
            f"- VIP Level 1 (10%)  : {sum(normalize_member_level(item['member_level']) == 'VIP ระดับ 1' for item in active_applications)}",
            f"- VIP Level 2 (13%)  : {sum(normalize_member_level(item['member_level']) == 'VIP ระดับ 2' for item in active_applications)}",
            f"- VIP Level 3 (20%)  : {sum(normalize_member_level(item['member_level']) == 'VIP ระดับ 3' for item in active_applications)}",
        ]
    )

    report_content = "\n".join(lines)
    with open(FILE_MEMBER_REPORT, "w", encoding="utf-8") as file:
        file.write(report_content)
        file.flush()
        os.fsync(file.fileno())
    return report_content


def add_member_application() -> None:
    print("\n--- สมัครสมาชิก ---")
    name = input_string("ชื่อ-นามสกุล (ไม่เกิน 32 ไบต์): ", max_length=32)
    member_level = input_string(
        "ระดับสมาชิก (ทั่วไป / vip1: ลด 10% / vip2: ลด 13% / vip3: ลด 20%): ",
        max_length=24,
        choices=[
            "ทั่วไป", "vip1", "vip2", "vip3",
            "VIP ระดับ 1", "VIP ระดับ 2", "VIP ระดับ 3",
        ],
    )
    member_level = normalize_member_level(member_level)

    applications = read_member_applications()
    user_id = max((item["user_id"] for item in applications), default=0) + 1
    applied_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    applications.append(
        {
            "user_id": user_id,
            "name": name,
            "legacy_phone": "",
            "member_level": member_level,
            "applied_at": applied_at,
            "is_active": True,
        }
    )
    write_member_applications(applications)
    generate_member_report(applications)
    log_action(f"Added member user ID: {user_id} ({name})")
    print(f"[✓] บันทึกสมาชิก ID USER {user_id} แล้ว")


def write_member_applications(applications: list[dict]) -> None:
    with open(FILE_CUSTOMERS, "wb") as file:
        for application in applications:
            file.write(
                pack_member_application(
                    application["user_id"],
                    application["name"],
                    application["member_level"],
                    application["applied_at"],
                    application["is_active"],
                    application.get("legacy_phone", ""),
                )
            )
        file.flush()
        os.fsync(file.fileno())


def update_member_application() -> None:
    applications = read_member_applications()
    user_id = input_int("ID USER ที่ต้องการแก้ไข: ", min_val=1)
    application = next(
        (item for item in applications if item["user_id"] == user_id and item["is_active"]),
        None,
    )
    if application is None:
        print("[!] ไม่พบสมาชิก หรือสมาชิกถูกลบแล้ว")
        return

    name = input("ชื่อ-สกุลใหม่ (Enter เพื่อคงเดิม): ").strip()
    if name:
        if len(name.encode("utf-8")) > 32:
            print("[!] ชื่อยาวเกิน 32 ไบต์ จึงคงชื่อเดิมไว้")
        else:
            application["name"] = name

    member_level = input(
        "ระดับสมาชิกใหม่ (ทั่วไป/vip1/vip2/vip3, Enter เพื่อคงเดิม): "
    ).strip()
    if member_level:
        normalized_level = normalize_member_level(member_level)
        if member_level == "ทั่วไป" or normalized_level in VIP_DISCOUNTS:
            application["member_level"] = normalized_level
        else:
            print("[!] ระดับสมาชิกไม่ถูกต้อง จึงคงระดับเดิมไว้")

    write_member_applications(applications)
    generate_member_report(applications)
    log_action(f"Updated member user ID: {user_id}")
    print("[✓] แก้ไขข้อมูลสมาชิกแล้ว")


def delete_member_application() -> None:
    applications = read_member_applications()
    user_id = input_int("ID USER ที่ต้องการลบ: ", min_val=1)
    remaining_applications = [
        item for item in applications if item["user_id"] != user_id
    ]
    if len(remaining_applications) == len(applications):
        print("[!] ไม่พบ ID USER นี้")
        return

    write_member_applications(remaining_applications)
    generate_member_report(remaining_applications)
    log_action(f"Deleted member user ID: {user_id}")
    print("[✓] ลบสมาชิกและข้อมูลที่เกี่ยวข้องแล้ว")


def member_application_management_menu() -> None:
    while True:
        print("\n--- การสมัครสมาชิก ---")
        print("1) Add (เพิ่ม)")
        print("2) Update (แก้ไข)")
        print("3) Delete (ลบ)")
        print("4) View (ดู)")
        print("5) Generate Report (.txt)")
        print("0) ย้อนกลับหน้าหลัก")
        choice = input("เลือกเมนู (0-5): ").strip()

        if choice == "1":
            add_member_application()
        elif choice == "2":
            update_member_application()
        elif choice == "3":
            delete_member_application()
        elif choice == "4":
            report = generate_member_report()
            print("\n" + report)
        elif choice == "5":
            report = generate_member_report()
            print(f"[✓] สร้างรายงาน '{FILE_MEMBER_REPORT}' สำเร็จแล้ว")
            print("\n" + report)
        elif choice == "0":
            break
        else:
            print("[!] ตัวเลือกไม่ถูกต้อง กรุณาเลือก 0 ถึง 5")


# ==========================================
# CRUD OPERATIONS
# ==========================================
def book_computer():
    booking_date_input = input("วันที่จอง (YYYY-MM-DD, Enter = วันนี้): ").strip()
    if booking_date_input:
        try:
            booking_date = datetime.strptime(booking_date_input, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            print("[!] รูปแบบวันที่ไม่ถูกต้อง กรุณาใช้ YYYY-MM-DD")
            return
    else:
        booking_date = datetime.now().strftime("%Y-%m-%d")

    print("ช่วงเวลาจอง:")
    for slot_id, (slot, hours) in BOOKING_SLOTS.items():
        print(f"{slot_id}) {slot} ({hours} ชั่วโมง)")
    slot_id = input_string("เลือกช่วงเวลา (1-2): ", max_length=1, choices=list(BOOKING_SLOTS))
    time_slot, hours = BOOKING_SLOTS[slot_id]

    zone_input = input_string(
        "Enter Zone (Standard/VIP; VIP, vip1, vip2, vip3): ",
        max_length=15,
        choices=["Standard", "VIP", "vip1", "vip2", "vip3"],
    ).casefold()
    selected_zone = normalize_computer_zone(zone_input)

    bookings = read_all_bookings()
    available = [
        (index, computer)
        for index, computer in enumerate(read_all_computers())
        if computer["is_active"]
        and computer["status"].lower() != "maint"
        and normalize_computer_zone(computer["zone"]) == selected_zone
        and not any(
            booking["is_active"]
            and booking["comp_id"] == computer["comp_id"]
            and booking["booking_date"] == booking_date
            and booking["time_slot"] == time_slot
            for booking in bookings
        )
    ]
    if not available:
        print(f"[!] ไม่มีเครื่องโซน {selected_zone} ว่างในช่วง {time_slot} วันที่ {booking_date}")
        return

    print(f"\nเครื่องว่างโซน {selected_zone} วันที่ {booking_date} ช่วง {time_slot}:")
    for _, computer in available:
        print(
            f"{computer['comp_id']} - {computer['pc_name']} "
            f"({computer['zone']})"
        )

    comp_id = input_int("CompID ที่ต้องการจอง: ", min_val=1)
    selected = next(
        ((index, computer) for index, computer in available if computer["comp_id"] == comp_id),
        None,
    )
    if selected is None:
        print("[!] ไม่พบเครื่องที่พร้อมให้จองตาม CompID นี้")
        return

    active_vip_members = [
        member
        for member in read_member_applications()
        if member["is_active"] and member_discount(member["member_level"]) > 0
    ]
    user_id = 0
    member_level = "ทั่วไป"
    if active_vip_members:
        print("\nสมาชิก VIP ที่ใช้สิทธิ์ส่วนลดได้:")
        for member in active_vip_members:
            discount = member_discount(member["member_level"])
            print(
                f"{member['user_id']} - {member['name']} "
                f"({member['member_level']}, ลด {discount:g}%)"
            )
    else:
        print("[!] ไม่มีสมาชิก VIP ที่ใช้งานอยู่")

    while True:
        user_id_text = input("ID USER VIP (กด Enter เพื่อข้าม): ").strip()
        if not user_id_text or user_id_text == "0":
            break
        if user_id_text.isdigit():
            user_id = int(user_id_text)
            member = next(
                (
                    item
                    for item in active_vip_members
                    if item["user_id"] == user_id
                ),
                None,
            )
            if member is not None:
                member_level = normalize_member_level(member["member_level"])
                break
            print("[!] ไม่พบ ID USER VIP ที่ใช้งานอยู่ กรุณากรอกใหม่ หรือกด Enter เพื่อข้าม")
        else:
            print("[!] กรุณากรอก ID USER เป็นตัวเลข หรือกด Enter เพื่อข้าม")

    discount_percent = member_discount(member_level)
    subtotal = hours * RATE_PER_HOUR
    total = subtotal * (1 - discount_percent / 100)
    booking_id = max((booking["booking_id"] for booking in bookings), default=0) + 1
    booked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    booking = {
        "booking_id": booking_id,
        "comp_id": comp_id,
        "user_id": user_id,
        "member_level": member_level,
        "hours": hours,
        "rate_per_hour": RATE_PER_HOUR,
        "discount_percent": discount_percent,
        "subtotal": subtotal,
        "total": total,
        "booked_at": booked_at,
        "booking_date": booking_date,
        "time_slot": time_slot,
        "is_active": True,
    }
    bookings.append(booking)
    write_all_bookings(bookings)

    log_action(f"Booked CompID: {comp_id} on {booking_date} {time_slot}")
    generate_report()
    print(
        f"[✓] จองสำเร็จวันที่ {booking_date} เวลา {time_slot} "
        f"({hours} ชั่วโมง x {RATE_PER_HOUR:.2f} บาท) "
        f"| สมาชิก: {member_level} | ส่วนลด: {discount_percent:g}% "
        f"| ยอดก่อนลด: {subtotal:.2f} บาท | ยอดสุทธิ: {total:.2f} บาท"
    )


def add_computer():
    print("\n--- Add / Book Computer ---")
    print("1) Add computer record (เพิ่มเครื่อง)")
    print("2) Book available computer (จองเครื่อง)")
    print("0) กลับ")
    choice = input("เลือกเมนูย่อย (0-2): ").strip()
    if choice == "1":
        add_computer_record()
    elif choice == "2":
        book_computer()
    elif choice != "0":
        print("[!] ตัวเลือกไม่ถูกต้อง กรุณาเลือก 0 ถึง 2")


def add_computer_record():
    """เพิ่มระเบียนคอมพิวเตอร์ใหม่ พร้อมตรวจสอบ Free Slots"""
    print("\n--- Add New Computer ---")
    comp_id = input_int("Enter CompID (e.g. 1011): ", min_val=1)
    
    records = read_all_computers()
    for r in records:
        if r["comp_id"] == comp_id:
            print(f"[!] Error: CompID {comp_id} มีอยู่ในระบบแล้ว ไม่สามารถเพิ่มซ้ำได้")
            return

    pc_name = input_string("Enter PC Name (e.g. PC-11): ", max_length=15)
    zone_input = input_string(
        "Enter Zone (Standard/VIP; vip, vip1, vip2, vip3): ",
        max_length=15,
        choices=["Standard", "VIP", "vip", "vip1", "vip2", "vip3"],
    )
    zone = normalize_computer_zone(zone_input)
    ram_gb = input_int("Enter RAM GB (e.g. 16, 32): ", min_val=1)
    rate_per_hr = input_float("Enter Rate (THB/Hour): ", min_val=0.0)
    status = input_string("Enter Status (Available/In Use/Maint): ", max_length=15, choices=["Available", "In Use", "Maint"])

    data = pack_computer_record(comp_id, pc_name, zone, ram_gb, rate_per_hr, status, True)

    # ค้นหา Slot ที่ถูก Soft Delete เพื่อเขียนทับ (Free List Reuse)
    free_slot_idx = None
    for idx, r in enumerate(records):
        if not r["is_active"]:
            free_slot_idx = idx
            break

    if free_slot_idx is not None:
        with open(FILE_COMPUTERS, "rb+") as f:
            f.seek(free_slot_idx * RECORD_SIZE)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        print(f"[✓] บันทึกข้อมูลคอมพิวเตอร์ใหม่สำเร็จ (เขียนทับ Free Slot ที่ {free_slot_idx})")
    else:
        with open(FILE_COMPUTERS, "ab") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        print("[✓] บันทึกข้อมูลคอมพิวเตอร์ใหม่ต่อท้ายไฟล์สำเร็จ")

    log_action(f"Added new computer CompID: {comp_id} ({pc_name})")


def update_computer():
    """2) Update แก้ไขระเบียนคอมพิวเตอร์"""
    print("\n--- Update Computer ---")
    comp_id = input_int("Enter CompID to Update: ", min_val=1)
    records = read_all_computers()
    found = False

    with open(FILE_COMPUTERS, "rb+") as f:
        for idx, r in enumerate(records):
            if r["comp_id"] == comp_id and r["is_active"]:
                found = True
                print(f"พบข้อมูลปัจจุบัน -> Name: {r['pc_name']} | Zone: {r['zone']} | RAM: {r['ram_gb']} | Rate: {r['rate_per_hr']} | Status: {r['status']}")
                print("(กด Enter หากไม่ต้องการแก้ไขค่านั้นๆ)")

                pc_name_in = input(f"New PC Name [{r['pc_name']}]: ").strip()
                pc_name = pc_name_in if pc_name_in else r['pc_name']

                zone_in = input(f"New Zone [{r['zone']}]: ").strip()
                zone = zone_in if zone_in else r['zone']

                ram_in = input(f"New RAM [{r['ram_gb']}]: ").strip()
                ram_gb = int(ram_in) if ram_in.isdigit() else r['ram_gb']

                rate_in = input(f"New Rate [{r['rate_per_hr']}]: ").strip()
                try:
                    rate_per_hr = float(rate_in) if rate_in else r['rate_per_hr']
                except ValueError:
                    rate_per_hr = r['rate_per_hr']

                status_in = input(f"New Status [{r['status']}]: ").strip()
                status = status_in if status_in else r['status']

                f.seek(idx * RECORD_SIZE)
                f.write(pack_computer_record(comp_id, pc_name, zone, ram_gb, rate_per_hr, status, True))
                f.flush()
                os.fsync(f.fileno())
                print("[✓] แก้ไขข้อมูลคอมพิวเตอร์เรียบร้อยแล้ว")
                log_action(f"Updated computer CompID: {comp_id}")
                break

    if not found:
        print("[!] ไม่พบ CompID นี้ หรือสถานะถูกลบไปแล้ว")


def delete_computer():
    """ลบระเบียนคอมพิวเตอร์และรายการจองที่อ้างถึงเครื่องนั้น"""
    print("\n--- Delete Computer ---")
    comp_id = input_int("Enter CompID to Delete: ", min_val=1)
    records = read_all_computers()
    remaining_records = [record for record in records if record["comp_id"] != comp_id]
    if len(remaining_records) == len(records):
        print("[!] ไม่พบ CompID นี้")
        return

    with open(FILE_COMPUTERS, "wb") as file:
        for record in remaining_records:
            file.write(
                pack_computer_record(
                    record["comp_id"], record["pc_name"], record["zone"],
                    record["ram_gb"], record["rate_per_hr"], record["status"],
                    record["is_active"],
                )
            )
        file.flush()
        os.fsync(file.fileno())

    bookings = read_all_bookings()
    remaining_bookings = [booking for booking in bookings if booking["comp_id"] != comp_id]
    if len(remaining_bookings) != len(bookings):
        write_all_bookings(remaining_bookings)

    log_action(f"Deleted computer CompID: {comp_id} and related bookings")
    generate_report()
    print(f"[✓] ลบคอมพิวเตอร์ CompID {comp_id} และรายการจองที่เกี่ยวข้องแล้ว")


def view_menu():
    """4) View เมนูย่อยสำหรับการดูข้อมูล"""
    while True:
        print("\n--- 4) View Sub-Menu ---")
        print("  4.1) ดูรายการเดียว (Single Record)")
        print("  4.2) ดูทั้งหมด (View All)")
        print("  4.3) ดูแบบกรอง (Filter by Zone)")
        print("  4.4) สถิติโดยสรุป (Summary Stats)")
        print("  0) ย้อนกลับเมนูก่อนหน้า")
        
        choice = input("เลือกเมนูย่อย (0, 1-4): ").strip()
        records = read_all_computers()

        if choice in ["1", "4.1"]:
            cid = input_int("Enter CompID: ", min_val=1)
            rec = next((r for r in records if r["comp_id"] == cid), None)
            if rec:
                print(f"\nID: {rec['comp_id']} | Name: {rec['pc_name']} | Zone: {rec['zone']} | RAM: {rec['ram_gb']}GB | Rate: {rec['rate_per_hr']:.2f} THB | Status: {rec['status']} | Active: {rec['is_active']}")
                log_action(f"Viewed single record CompID: {cid}")
            else:
                print("[!] ไม่พบข้อมูล")

        elif choice in ["2", "4.2"]:
            report_text = generate_report()
            table_part = report_text.split("Summary")[0]
            print("\n" + table_part)
            log_action("Viewed all computer records")

        elif choice in ["3", "4.3"]:
            zone_filter = input_string("กรอกโซนที่ต้องการกรอง (Standard/VIP): ").strip().lower()
            filtered = [r for r in records if r["zone"].lower() == zone_filter and r["is_active"]]
            print(f"\n--- รายการในโซน {zone_filter.upper()} (Active) ---")
            if filtered:
                for r in filtered:
                    print(f"ID: {r['comp_id']} | Name: {r['pc_name']} | RAM: {r['ram_gb']}GB | Rate: {r['rate_per_hr']:.2f} | Status: {r['status']}")
            else:
                print("[!] ไม่พบข้อมูลในโซนนี้")
            log_action(f"Filtered records by zone: {zone_filter}")

        elif choice in ["4", "4.4"]:
            report_text = generate_report()
            summary_part = report_text[report_text.find("Summary"):]
            print("\n" + summary_part)
            log_action("Viewed summary statistics")

        elif choice == "0":
            break
        else:
            print("[!] ตัวเลือกไม่ถูกต้อง กรุณาเลือก 0 ถึง 4")


# ==========================================
# MAIN PROGRAM LOOP
# ==========================================
def computer_management_menu() -> None:
    while True:
        print("\n--- จองคอมพิวเตอร์ ---")
        print("1) Add (เพิ่ม)")
        print("2) Update (แก้ไข)")
        print("3) Delete (ลบ)")
        print("4) View (ดู)")
        print("5) Generate Report (.txt)")
        print("0) ย้อนกลับหน้าหลัก")
        choice = input("เลือกเมนู (0-5): ").strip()

        if choice == "1":
            add_computer()
        elif choice == "2":
            update_computer()
        elif choice == "3":
            delete_computer()
        elif choice == "4":
            view_menu()
        elif choice == "5":
            log_action("Generated computer report manually")
            report = generate_report()
            print(f"[✓] สร้างรายงาน '{FILE_REPORT}' สำเร็จแล้ว")
            print("\n" + report)
        elif choice == "0":
            break
        else:
            print("[!] ตัวเลือกไม่ถูกต้อง กรุณาเลือก 0 ถึง 5")


def show_report_tables(report_text: str) -> None:
    """แสดงเฉพาะส่วนตาราง (ตัดส่วน Summary ท้ายรายงานออก) ให้ดูผ่าน terminal ได้ทันที"""
    print("\n" + report_text.split("\nSummary")[0].rstrip())


def main():
    init_data_files()

    while True:
        print("\n==========================================")
        print(" Cyber Cafe Booking System (Python File I/O)")
        print("==========================================")
        print("1) จองคอมพิวเตอร์")
        print("2) จำลองลูกค้ารายงานปัญหา")
        print("3) การสมัครสมาชิก")
        print("0) ออก")

        choice = input("\nเลือกเมนูหลัก (0-3): ").strip()

        if choice == "1":
            show_report_tables(generate_report())
            computer_management_menu()
        elif choice == "2":
            show_report_tables(generate_issue_report())
            issue_management_menu(read_all_computers)
        elif choice == "3":
            show_report_tables(generate_member_report())
            member_application_management_menu()
        elif choice == "0":
            log_action("Exited application safely")
            report = generate_report()
            print(f"\n[✓] สร้างรายงานอัตโนมัติ ({FILE_REPORT}) flush/sync และปิดโปรแกรมเรียบร้อยแล้ว")
            break
        else:
            print("[!] เมนูไม่ถูกต้อง กรุณากรอกตัวเลข 0 ถึง 3")


if __name__ == "__main__":
    main()