# Lampang drought WebGIS

หน้าเฝ้าระวังความชื้นดินและฝนของจังหวัดลำปาง มีข้อมูลสองชั้น

| ชั้นข้อมูล | ช่วงเวลา | แหล่ง | การอัปเดต |
|---|---|---|---|
| ฝน, MAI, SPEI, ETo รายตำบล | 10 วัน | กรมอุตุนิยมวิทยา (TMD) ผ่าน Colab notebook → Google Sheet | เมื่อรัน notebook |
| แนวโน้มฤดูกาล ฝน/อุณหภูมิ (tercile) | 1–5 เดือน + ฤดูกาล 3 เดือน | Copernicus C3S multi-system | GitHub Actions ทุกวันที่ 14 และ 16 ของเดือน |

## แนวโน้มฤดูกาล C3S

`scripts/c3s_seasonal_lampang.py` ทำงานตามขั้นตอนนี้
1. ดึงข้อมูลพยากรณ์รายเดือนของทุกศูนย์ใน C3S พร้อม hindcast ช่วง 1993–2016 สำหรับกรอบรอบลำปาง ระบบจะหาเลขรุ่นของแต่ละศูนย์ให้เองจาก CDS constraints
2. คำนวณ tercile ของแต่ละระบบด้วยวิธีเดียวกับ C3S แล้วหาค่าเฉลี่ยแบบน้ำหนักเท่ากันเป็นค่า multi-system
3. คำนวณอุณหภูมิอีกชุดแบบ **ปรับแนวโน้ม (trend-adjusted)** โดยเลื่อนค่าปกติตามแนวโน้มเชิงเส้นของ hindcast มาที่ปีพยากรณ์ เพื่อแยกผลของภาวะโลกร้อนออกจากสัญญาณฤดูกาล
4. เขียนผลลง `data/c3s_seasonal.json` ซึ่ง `index.html` อ่านไปแสดงเป็นตาราง กราฟความเห็นตรงกันของแต่ละระบบ และกริด 1° บนแผนที่

### ตั้งค่าครั้งแรก
1. สมัครบัญชีที่ https://cds.climate.copernicus.eu แล้วคัดลอก Personal Access Token จากหน้า profile
2. เปิดหน้า dataset **seasonal-monthly-single-levels** แล้วกดยอมรับ licence ถ้าไม่ยอมรับ ทุกคำขอจะได้ 403
3. ใน GitHub ไปที่ repo → Settings → Secrets and variables → Actions แล้วเพิ่ม secret ชื่อ `CDSAPI_KEY`
4. ไปที่ Actions → **C3S seasonal outlook (Lampang)** → Run workflow รอบแรกต้องดึง hindcast ทุกระบบ ใช้เวลาตามคิวของ CDS ซึ่งอาจนานหลายสิบนาที รอบถัดไปจะใช้ hindcast จาก cache

### รันบนเครื่องตัวเอง
```bash
pip install -r scripts/requirements-c3s.txt
export CDSAPI_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
python scripts/c3s_seasonal_lampang.py --init 2026-10            # ทุกศูนย์
python scripts/c3s_seasonal_lampang.py --init 2026-10 --centres ecmwf,ukmo
```

### ข้อควรรู้
- ศูนย์ที่ดึงข้อมูลไม่สำเร็จจะถูกข้ามและแสดงชื่อท้ายส่วนแนวโน้มฤดูกาล ถ้าดึงไม่สำเร็จเลยสักระบบ ไฟล์เดิมจะไม่ถูกเขียนทับ
- ถ้าเรียก CDS constraints ไม่ได้ สคริปต์จะใช้ `FALLBACK_SYSTEMS` ในสคริปต์แทน ควรแก้ค่านี้เมื่อ C3S ประกาศอัปเกรดระบบ
- ค่าเฉลี่ยทั้งจังหวัดใช้กริด 1° ช่วง 17–19°N, 99–100°E ถ่วงน้ำหนักด้วย cos(lat) ปรับได้ที่ `PROVINCE_LATS` / `PROVINCE_LONS`
- เป็นข้อมูลประกอบการวางแผนล่วงหน้า ไม่ใช่การพยากรณ์อย่างเป็นทางการ ควรใช้ร่วมกับประกาศของกรมอุตุนิยมวิทยา
