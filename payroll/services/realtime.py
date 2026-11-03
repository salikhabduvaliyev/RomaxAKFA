import logging
import requests
from decimal import Decimal
from datetime import datetime, date

logger = logging.getLogger(__name__)

class RealtimeMoySkladEngine:
    """
    MoySklad API orqali ma'lumotlarni hech qanday modellarga yozmasdan,
    to'g'ridan-to'g'ri REAL TIME rejimida hisoblab beruvchi dvigatel (Romax Akfa).
    """

    def __init__(self, token=None, base_url="https://api.moysklad.ru/api/remap/1.2"):
        from payroll.models import MoySkladConfig
        config = MoySkladConfig.get_solo()
        self.token = token if token is not None else config.token
        self.base_url = (base_url or config.base_url).rstrip('/')
        self.timeout = 25

    @property
    def headers(self):
        return {
            "Authorization": f"Bearer {self.token.strip()}" if self.token else "",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
        }

    def get_realtime_payroll(self, month, date_from=None, date_to=None, total_plans=None):
        from payroll.models import KPIRules, ProductionCategory

        total_plans = total_plans or {}

        # 1. Sana oralig'i
        if not date_from or not date_to:
            try:
                y, m = map(int, month.split('-'))
                if m == 12:
                    next_m = date(y + 1, 1, 1)
                else:
                    next_m = date(y, m + 1, 1)
                d_from = date(y, m, 1)
                d_to = date.fromordinal(next_m.toordinal() - 1)
            except Exception:
                d_from = date(2026, 10, 1)
                d_to = date(2026, 10, 31)
        else:
            d_from = date_from
            d_to = date_to

        if isinstance(d_from, str):
            try:
                d_from = datetime.strptime(d_from[:10], '%Y-%m-%d').date()
            except Exception:
                d_from = date(2026, 10, 1)
        if isinstance(d_to, str):
            try:
                d_to = datetime.strptime(d_to[:10], '%Y-%m-%d').date()
            except Exception:
                d_to = date(2026, 10, 31)

        d_from_str = f"{d_from} 00:00:00"
        d_to_str = f"{d_to} 23:59:59"

        rules = KPIRules.get_solo()
        categories = list(ProductionCategory.objects.all())

        # 2. XODIMLAR: MoySklad «Моя компания → Сотрудники» dan real-time
        employees = []
        if self.token:
            try:
                # 1) Faol xodimlar
                url_emp = f"{self.base_url}/entity/employee?limit=100&filter=archived=false"
                resp_emp = requests.get(url_emp, headers=self.headers, timeout=self.timeout)
                rows = resp_emp.json().get('rows', []) if resp_emp.status_code == 200 else []

                # 2) Arxivlangan xodimlar (oy davomida ishlagan yoki avans olganlar tushib qolmasligi uchun)
                try:
                    resp_arch = requests.get(f"{self.base_url}/entity/employee?limit=100&filter=archived=true", headers=self.headers, timeout=self.timeout)
                    if resp_arch.status_code == 200:
                        rows.extend(resp_arch.json().get('rows', []))
                except Exception:
                    pass

                for i, r in enumerate(rows, 1):
                        name = (r.get('name') or r.get('fullName') or '').strip()
                        if name.lower() == 'administrator':
                            continue
                        pos = (r.get('position') or '').strip()

                        # OKLAD: xodim kartochkasidagi salary maydoni
                        salary_obj = r.get('salary', {})
                        oklad_val = 0.0
                        if isinstance(salary_obj, dict):
                            oklad_val = float(salary_obj.get('value', 0) or 0)
                        elif isinstance(salary_obj, (int, float)):
                            oklad_val = float(salary_obj)

                        # Guruh va bo'limlarni aniqlash:
                        # 1) Sotuv operatorlari (оператор)
                        # 2) Sotuv menejerlari va sotuvchilar (менеджер, рахбар, юритувчи, сотувчи)
                        # 3) Монтажчилар (монтаж) -> ALOHIDA BO'LIM! PVX yoki Termoga bog'liq emas!
                        # 4) PVX bo'limi (бригадир пвх, цех, уста, мастер)
                        # 5) TERMO bo'limi (бригадир термо, термо)
                        # 6) Boshqa (faqat oklad)
                        combined = f"{name} {pos}".lower()
                        pos_lower = pos.lower()
                        group = "bosh"
                        dept = pos or "Boshqa"

                        # 1) Sotuv operatorlari
                        if "оператор" in combined:
                            group = "op"
                            dept = "Sotuv operatori"
                        # 2) Sotuvchilar — Foydalanuvchi talabi: Сотрудники lavozimi «сотувчи» bo'lsa olinadi
                        elif "сотувчи" in pos_lower or "sotuvchi" in pos_lower:
                            group = "men"
                            dept = "Сотувчи"
                        # 3) Монтажчилар — ALOHIDA BO'LIM! PVX yoki Termoga bog'liq emas!
                        elif "монтаж" in combined:
                            group = "ish"
                            dept = "Монтаж"
                        # 4) TERMO bo'limi
                        elif any(k in combined for k in ["термо", "бригадир терм"]):
                            group = "ish"
                            dept = "TERMO bo'limi"
                        # 5) PVX bo'limi
                        elif any(k in combined for k in ["цех", "уста", "бригадир пвх", "мастер"]):
                            group = "ish"
                            dept = "PVX bo'limi"
                        # 6) Boshqa (faqat oklad: рахбар, иш юритувчи, ошпаз, омборчи, чизмачи, шофёр va b.)
                        else:
                            group = "bosh"
                            dept = pos or "Boshqa (oklad)"

                        employees.append({
                            'id': r.get('id'),
                            'ms_id': r.get('id'),
                            'name': name,
                            'position': pos or dept,
                            'group': group,
                            'dept': dept,
                            'oklad': oklad_val,
                            'manager_rate': 1.0,
                            'order_num': i,
                            'is_active': True
                        })
            except Exception as e:
                logger.exception(f"Error fetching realtime employees: {e}")

        # Faol xodimlar sonini guruhlar bo'yicha aniqlash
        op_count = sum(1 for e in employees if e['is_active'] and e['group'] == 'op') or 1
        men_count = sum(1 for e in employees if e['is_active'] and e['group'] == 'men') or 1
        pvh_count = sum(1 for e in employees if e['is_active'] and e['dept'] == 'PVX bo\'limi') or 1
        termo_count = sum(1 for e in employees if e['is_active'] and e['dept'] == 'TERMO bo\'limi') or 1
        montaj_count = sum(1 for e in employees if e['is_active'] and e['dept'] == 'Монтаж') or 1

        # 3. ZAKAZLAR: MoySklad «Продажи → Заказы покупателей» dan real-time
        emp_op_sales = {}   # {emp_id: {"order_count": int, "contract_sum": float}}
        emp_men_sales = {}  # {emp_id: {"order_count": int, "contract_sum": float}}
        emp_prod_pvh = {}   # {emp_id: float (m2)} - Faqat ПВХ polyasi
        emp_prod_termo = {} # {emp_id: float (m2)} - ТЕРМО, Чемпион, АЛДОХ polyalari
        emp_mont_sqm = {}   # {emp_id: float (m2)} - АЛДОХ, Чемпион, ТЕРМО, ПВХ qismlari
        emp_prod_order_cnt = {} # {emp_id: int}
        emp_mont_order_cnt = {} # {emp_id: int}
        
        production_live_facts = {
            'PVX': 0.0,
            'Termo': 0.0,
            'Chempion': 0.0,
            'Alyuminiy': 0.0,
            'Montaj': 0.0
        }

        if self.token:
            try:
                # 0. Barcha otgruzkalarni (demand) olib, zakazlarga bog'lash
                demands_by_order_id = {}
                try:
                    dem_offset = 0
                    while True:
                        dem_url = f"{self.base_url}/entity/demand?expand=customerOrder&limit=100&offset={dem_offset}"
                        resp_dem = requests.get(dem_url, headers=self.headers, timeout=self.timeout)
                        if resp_dem.status_code != 200:
                            break
                        dm_rows = resp_dem.json().get('rows', [])
                        for dm_row in dm_rows:
                            dm_moment = (dm_row.get('moment', '') or '')[:10]
                            cord = dm_row.get('customerOrder', {})
                            cord_href = cord.get('meta', {}).get('href', '') if cord else ''
                            cord_id = cord_href.split('/')[-1] if cord_href else None
                            if cord_id and dm_moment:
                                try:
                                    dm_date = datetime.strptime(dm_moment, '%Y-%m-%d').date()
                                    demands_by_order_id.setdefault(cord_id, []).append(dm_date)
                                except Exception:
                                    pass
                        if len(dm_rows) < 100:
                            break
                        dem_offset += 100
                except Exception as e_dem:
                    logger.warning(f"Error fetching demands map: {e_dem}")

                ord_offset = 0
                all_orders_data = []
                while True:
                    orders_url = f"{self.base_url}/entity/customerorder?expand=demands,state,owner&limit=100&offset={ord_offset}&order=moment,desc"
                    resp_ord = requests.get(orders_url, headers=self.headers, timeout=self.timeout)
                    if resp_ord.status_code != 200:
                        break
                    ord_rows = resp_ord.json().get('rows', [])
                    all_orders_data.extend(ord_rows)
                    if len(ord_rows) < 100:
                        break
                    ord_offset += 100

                for order in all_orders_data:
                        state_name = order.get('state', {}).get('name', '')
                        demands = order.get('demands', [])
                        shipped_sum = order.get('shippedSum', 0) or 0
                        order_sum = (order.get('sum', 0) or 0) / 100.0
                        desc = (order.get('description', '') or '').upper()

                        order_moment_str = (order.get('moment', '') or '')[:10]
                        try:
                            order_date = datetime.strptime(order_moment_str, '%Y-%m-%d').date() if order_moment_str else None
                        except Exception:
                            order_date = None

                        is_order_in_period = bool(order_date and (d_from <= order_date <= d_to))

                        attrs = {a.get('name'): a.get('value') for a in order.get('attributes', [])}
                        contract_sum = float(attrs.get('Шартнома суммаси') or order_sum or 0)

                        # A) Sotuv operatori (Lavozimidan qat'iy nazar hamma operator/sotuvchi bo'lishi mumkin)
                        if is_order_in_period:
                            op_attr = attrs.get('Сотув оператори')
                            if isinstance(op_attr, dict):
                                op_href = op_attr.get('meta', {}).get('href', '')
                                op_id = op_href.split('/')[-1] if op_href else None
                                op_name = (op_attr.get('name', '') or '').upper()
                                for emp in employees:
                                    if emp['ms_id'] == op_id or (op_name and op_name in emp['name'].upper()):
                                        slot = emp_op_sales.setdefault(emp['id'], {"order_count": 0, "contract_sum": 0.0})
                                        slot['order_count'] += 1
                                        slot['contract_sum'] += contract_sum
                                        break

                            # B) Sotuvchilar (Menejerlar) — umumiy xodimlardan
                            men_attr = attrs.get('Сотув менеджери')
                            matched_men = False
                            if isinstance(men_attr, dict):
                                men_href = men_attr.get('meta', {}).get('href', '')
                                men_id = men_href.split('/')[-1] if men_href else None
                                men_name = (men_attr.get('name', '') or '').upper()
                                for emp in employees:
                                    if emp['ms_id'] == men_id or (men_name and men_name in emp['name'].upper()):
                                        slot = emp_men_sales.setdefault(emp['id'], {"order_count": 0, "contract_sum": 0.0})
                                        slot['order_count'] += 1
                                        slot['contract_sum'] += contract_sum
                                        matched_men = True
                                        break

                            # C) Agar «Сотув менеджери» атрибути бўш бўлса, фақат тавсифда (description) сотувчи исми ёзилган бўлса аниқланади
                            # DIQQAT: owner_id (ҳужжатни киритган оператор/админ, масалан Тимур Турдиев) сотувчи ҳисобланмайди!
                            if not matched_men and desc:
                                for emp in employees:
                                    emp_first = emp['name'].split()[0].upper()
                                    if len(emp_first) >= 4 and emp_first in desc:
                                        slot = emp_men_sales.setdefault(emp['id'], {"order_count": 0, "contract_sum": 0.0})
                                        slot['order_count'] += 1
                                        slot['contract_sum'] += contract_sum
                                        matched_men = True
                                        break

                        # D) ISHLAB CHIQARISH VA MONTAJ:
                        # Foydalanuvchi talabi:
                        # qachonki Заказ покупателя otgruzga bolsa otgruzka sanasi boyicha Заказ покупателя dagi malumotlar hisobga kirishi kerak
                        # agar otgruzga bolmasa ulanmagan bolsa bunda kirmaydi hisoblanmaydi Заказ покупателя
                        order_id = order.get('id')
                        all_demand_dates = list(demands_by_order_id.get(order_id, []))
                        for d in demands:
                            dm = (d.get('moment', '') or '')[:10]
                            if dm:
                                try:
                                    dd = datetime.strptime(dm, '%Y-%m-%d').date()
                                    if dd not in all_demand_dates:
                                        all_demand_dates.append(dd)
                                except Exception:
                                    pass

                        has_demands = len(all_demand_dates) > 0
                        is_production_in_period = False
                        if has_demands:
                            # Faqat OTGRUZKA SANASI tanlangan davr oralig'ida bo'lsa hisobga olinadi
                            is_production_in_period = any(d_from <= dd <= d_to for dd in all_demand_dates)
                        else:
                            # Otgruzga ulanmagan bo'lsa, hisobga kirmaydi / hisoblanmaydi
                            is_production_in_period = False

                        if is_production_in_period:
                            pvh_val = float(attrs.get('ПВХ') or 0)
                            termo_val = float(attrs.get('ТЕРМО') or 0)
                            chemp_val = float(attrs.get('Чемпион') or 0)
                            aldox_val = float(attrs.get('АЛДОХ') or 0)
                            mont_val = pvh_val + termo_val + chemp_val + aldox_val

                            # 1) ПВХ polyasi -> Faqat «Ишлаб чикариш булими (ПВХ)» xodimiga
                            if pvh_val > 0:
                                production_live_facts['PVX'] += pvh_val
                                pvh_emp_attr = attrs.get('Ишлаб чикариш булими (ПВХ)')
                                if isinstance(pvh_emp_attr, dict):
                                    pvh_eid = pvh_emp_attr.get('meta', {}).get('href', '').split('/')[-1]
                                    pvh_name = (pvh_emp_attr.get('name', '') or '').upper()
                                    for emp in employees:
                                        if emp['ms_id'] == pvh_eid or (pvh_name and pvh_name in emp['name'].upper()):
                                            emp_prod_pvh[emp['id']] = emp_prod_pvh.get(emp['id'], 0.0) + pvh_val
                                            emp_prod_order_cnt[emp['id']] = emp_prod_order_cnt.get(emp['id'], 0) + 1
                                            break

                            # 2) ТЕРМО, Чемпион, АЛДОХ polyalari -> Faqat «Ишлаб чикариш булими (ТЕРМО)» xodimiga
                            t_sum = termo_val + chemp_val + aldox_val
                            if t_sum > 0:
                                production_live_facts['Termo'] += termo_val
                                production_live_facts['Chempion'] += chemp_val
                                production_live_facts['Alyuminiy'] += aldox_val
                                termo_emp_attr = attrs.get('Ишлаб чикариш булими (ТЕРМО)')
                                if isinstance(termo_emp_attr, dict):
                                    termo_eid = termo_emp_attr.get('meta', {}).get('href', '').split('/')[-1]
                                    termo_name = (termo_emp_attr.get('name', '') or '').upper()
                                    for emp in employees:
                                        if emp['ms_id'] == termo_eid or (termo_name and termo_name in emp['name'].upper()):
                                            emp_prod_termo[emp['id']] = emp_prod_termo.get(emp['id'], 0.0) + t_sum
                                            emp_prod_order_cnt[emp['id']] = emp_prod_order_cnt.get(emp['id'], 0) + 1
                                            break

                            # 3) Монтаж: АЛДОХ, Чемпион, ТЕРМО, ПВХ qismini oladi
                            if mont_val > 0:
                                production_live_facts['Montaj'] += mont_val
                                mont_emp_attr = attrs.get('Монтаж командаси')
                                if isinstance(mont_emp_attr, dict):
                                    mont_eid = mont_emp_attr.get('meta', {}).get('href', '').split('/')[-1]
                                    mont_name = (mont_emp_attr.get('name', '') or '').upper()
                                    for emp in employees:
                                        if emp['ms_id'] == mont_eid or (mont_name and mont_name in emp['name'].upper()):
                                            emp_mont_sqm[emp['id']] = emp_mont_sqm.get(emp['id'], 0.0) + mont_val
                                            emp_mont_order_cnt[emp['id']] = emp_mont_order_cnt.get(emp['id'], 0) + 1
                                            break

            except Exception as e:
                logger.exception(f"Error fetching realtime orders: {e}")

        # 4. AVANSLAR: MoySklad kassa chiqimlaridan (FAQAT «ОЙЛИК ИШ ХАКИ» moddasi va «Дата начисления» bo'yicha real-time)
        # 4. AVANSLAR: MoySklad kassa chiqimlari (cashout) va to'lov chiqimlari (paymentout) dan
        # Foydalanuvchi talabi: FAQAT «ОЙЛИК ИШ ХАКИ» moddasi va tanlangan oylik davr ichida to'liq hisobga olinadi
        advances_by_emp = {}
        if self.token:
            try:
                d_from_str = f"{d_from} 00:00:00"
                d_to_str = f"{d_to} 23:59:59"

                for ep in ['cashout', 'paymentout']:
                    offset = 0
                    while True:
                        c_url = f"{self.base_url}/entity/{ep}?filter=moment>={d_from_str};moment<={d_to_str}&expand=agent,expenseItem&limit=100&offset={offset}"
                        c_resp = requests.get(c_url, headers=self.headers, timeout=self.timeout)
                        if c_resp.status_code != 200:
                            logger.warning(f"Error fetching {ep} advances: {c_resp.status_code} {c_resp.text}")
                            break

                        rows = c_resp.json().get('rows', [])
                        for row in rows:
                            summa = (row.get('sum', 0) or 0) / 100.0
                            desc = (row.get('description', '') or '').upper()
                            moment_str = (row.get('moment', '') or '')[:10]

                            exp_obj = row.get('expenseItem', {}) or {}
                            exp_name = (exp_obj.get('name', '') or '').upper()
                            exp_href = exp_obj.get('meta', {}).get('href', '')

                            # Foydalanuvchi talabi: FAQAT «ОЙЛИК ИШ ХАКИ» moddasi bo'yicha berilgan avanslar olinadi!
                            is_salary_expense = (
                                ('ОЙЛИК' in exp_name and 'ИШ ХАКИ' in exp_name) or 
                                '144eaa73-bfe5-11f1-0a80-112e001725cd' in exp_href or 
                                exp_name == 'ОЙЛИК ИШ ХАКИ'
                            )
                            if not is_salary_expense:
                                continue

                            # Davr tekshiruvi (Hujjat vaqti bo'yicha davr oralig'ida bo'lishi shart)
                            try:
                                moment_date = datetime.strptime(moment_str[:10], '%Y-%m-%d').date()
                            except Exception:
                                moment_date = None

                            if not moment_date or not (d_from <= moment_date <= d_to):
                                continue

                            agent_obj = row.get('agent', {}) or {}
                            agent_meta = agent_obj.get('meta', {}) or {}
                            agent_href = agent_meta.get('href', '')
                            agent_id = agent_href.split('/')[-1] if agent_href else None
                            agent_name = (agent_obj.get('name', '') or '').strip().upper()

                            matched = False
                            # 1) Aniq MoySklad agent ID orqali bog'lash
                            if agent_id:
                                for emp in employees:
                                    if emp['ms_id'] == agent_id:
                                        advances_by_emp[emp['id']] = advances_by_emp.get(emp['id'], 0.0) + summa
                                        matched = True
                                        break

                            # 2) Agent ismi bo'yicha bog'lash
                            if not matched and agent_name:
                                for emp in employees:
                                    emp_name_u = emp['name'].upper()
                                    if emp_name_u == agent_name or emp_name_u in agent_name or agent_name in emp_name_u:
                                        advances_by_emp[emp['id']] = advances_by_emp.get(emp['id'], 0.0) + summa
                                        matched = True
                                        break

                            # 3) Ism prefiksi / familiyasi bo'yicha bog'lash
                            if not matched and agent_name:
                                ag_base = agent_name.split('-')[0].strip()
                                for emp in employees:
                                    emp_name_u = emp['name'].upper()
                                    emp_base = emp_name_u.split('-')[0].strip()
                                    if (len(emp_base) >= 4 and emp_base in agent_name) or (len(ag_base) >= 4 and ag_base in emp_name_u):
                                        advances_by_emp[emp['id']] = advances_by_emp.get(emp['id'], 0.0) + summa
                                        matched = True
                                        break

                            # 4) Description ichida xodim ko'rsatilgan bo'lsa
                            if not matched and desc:
                                for emp in employees:
                                    emp_first = emp['name'].split()[0].upper()
                                    if len(emp_first) >= 4 and emp_first in desc:
                                        advances_by_emp[emp['id']] = advances_by_emp.get(emp['id'], 0.0) + summa
                                        matched = True
                                        break

                            # 5) Agar agent xodimlar ro'yxatida bo'lmasa ham, «ОЙЛИК ИШ ХАКИ» avansi yo'qolmasligi uchun uni xodim qilib qo'shish
                            if not matched and agent_name:
                                new_id = agent_id or f"adv-emp-{len(employees)+1}"
                                new_emp = {
                                    'id': new_id,
                                    'ms_id': new_id,
                                    'name': agent_name,
                                    'position': "Xodim",
                                    'group': "bosh",
                                    'dept': "Boshqa (oklad)",
                                    'oklad': 0.0,
                                    'manager_rate': 1.0,
                                    'order_num': len(employees) + 1,
                                    'is_active': True
                                }
                                employees.append(new_emp)
                                advances_by_emp[new_id] = advances_by_emp.get(new_id, 0.0) + summa
                                matched = True

                        if len(rows) < 100:
                            break
                        offset += 100
            except Exception as e:
                logger.exception(f"Error fetching realtime advances: {e}")

        # 5. ISHLAB CHIQARISH KATEGORIYALARI
        cats_result = []
        for c in categories:
            c_name_u = c.name.upper()
            if 'PVX' in c_name_u:
                live_fact = production_live_facts['PVX']
                w_count = pvh_count
                dept_label = "Ишлаб чикариш булими (ПВХ)"
                poly_label = "ПВХ"
            elif 'TERMO' in c_name_u:
                live_fact = production_live_facts['Termo']
                w_count = termo_count
                dept_label = "Ишлаб чикариш булими (ТЕРМО)"
                poly_label = "ТЕРМО"
            elif 'CHEMP' in c_name_u:
                live_fact = production_live_facts['Chempion']
                w_count = termo_count
                dept_label = "Ишлаб чикариш булими (ТЕРМО)"
                poly_label = "Чемпион"
            elif 'ALYUM' in c_name_u or 'ALDOX' in c_name_u:
                live_fact = production_live_facts['Alyuminiy']
                w_count = termo_count
                dept_label = "Ишлаб чикариш булими (ТЕРМО)"
                poly_label = "АЛДОХ"
            elif 'MONT' in c_name_u or 'МОНТ' in c_name_u:
                live_fact = production_live_facts['Montaj']
                w_count = montaj_count
                dept_label = "Монтаж бўлими (Алоҳида)"
                poly_label = "Монтаж"
            else:
                live_fact = float(c.fact)
                w_count = 1
                dept_label = "Boshqa"
                poly_label = c.name

            fact_to_use = live_fact if live_fact > 0 else float(c.fact)

            cats_result.append({
                'id': c.id,
                'name': c.name,
                'dept_label': dept_label,
                'poly_label': poly_label,
                'prev_month_volume': float(c.prev_month_volume),
                'growth_percent': c.growth_percent,
                'plan': float(c.plan),
                'fact': round(fact_to_use, 2),
                'tarif': float(c.tarif),
                'workers_count': w_count
            })

        # 6. PLANLAR VA TARIFLARNI REAL TIME TAQSIMLASH:
        tot_plan_op = float(total_plans.get('op') or 2000000000.0)    # masalan 2 mlrd so'm
        tot_plan_men = float(total_plans.get('men') or 2400000000.0) # masalan 2.4 mlrd so'm
        tarif_pvh = float(total_plans.get('tarif_pvh') or 60000.0)    # so'm/m2
        tarif_termo = float(total_plans.get('tarif_termo') or 80000.0) # so'm/m2
        tarif_montaj = float(total_plans.get('tarif_montaj') or 50000.0) # so'm/m2

        selected_op_ids = total_plans.get('selected_op_ids')
        if selected_op_ids is not None:
            # Faqat haqiqiy UUID xodim ID lari olinadi (tasodifiy kichik sonli indekslar emas!)
            selected_op_ids = [str(x).strip() for x in selected_op_ids if str(x).strip() and len(str(x).strip()) > 5]
        else:
            # Default: xodimlar ichidan lavozimida operator bo'lganlar
            selected_op_ids = [
                str(e['id']) for e in employees 
                if e['is_active'] and "оператор" in f"{e['name']} {e['position']}".lower()
            ]

        selected_men_ids = total_plans.get('selected_men_ids')
        if selected_men_ids is not None:
            # Faqat haqiqiy UUID xodim ID lari olinadi (tasodifiy kichik sonli indekslar emas!)
            selected_men_ids = [str(x).strip() for x in selected_men_ids if str(x).strip() and len(str(x).strip()) > 5]
        else:
            # Default: xodimlar ichidan lavozimida sotuvchi yoki menejer bo'lganlar
            selected_men_ids = [
                str(e['id']) for e in employees 
                if e['is_active'] and any(k in f"{e['name']} {e['position']}".lower() for k in ["сотувчи", "sotuvchi", "менеджер", "рахбар"])
            ]

        op_target_count = len(selected_op_ids)
        men_target_count = len(selected_men_ids)

        ind_plan_op = round(tot_plan_op / op_target_count) if op_target_count > 0 else 0
        ind_plan_men = round(tot_plan_men / men_target_count) if men_target_count > 0 else 0

        # 7. ISHLAB CHIQARISH VA MONTAJNI ALOHIDA LAYOUTLARGA AJRATISH
        # Foydalanuvchi talabi:
        # faqat ishlab chiqarganlar alohida layout da va montaj alohida hisoblanib berilgan tariflar boyicha hisoblanib kpi hisoblanadi
        # yani bitta hodim ham ishlab chiqaruvchi ham montaj qilish hisobi olinadi
        production_summary = []
        montaj_summary = []

        for emp in employees:
            eid = emp['id']
            comb_lower = f"{emp['name']} {emp['position']}".lower()

            p_m2 = round(emp_prod_pvh.get(eid, 0.0), 2)
            t_m2 = round(emp_prod_termo.get(eid, 0.0), 2)
            m_m2 = round(emp_mont_sqm.get(eid, 0.0), 2)

            total_prod_m2 = round(p_m2 + t_m2, 2)
            prod_bonus = round(p_m2 * tarif_pvh + t_m2 * tarif_termo)
            montaj_bonus = round(m_m2 * tarif_montaj)

            emp_oklad = emp['oklad']

            # A) FAQAT ISHLAB CHIQARGANLAR (ПВХ yoki ТЕРМО hajmi borlar yoki sex/ustalar)
            # Montajchi bu yerga kirmaydi, agar uning o'zida PVX yoki Termo hajmi bo'lmasa!
            is_prod_worker = (
                total_prod_m2 > 0 or 
                (any(k in comb_lower for k in ["цех", "уста", "пвх", "термо", "бригадир", "мастер"]) and "монтаж" not in comb_lower)
            )
            if is_prod_worker:
                if t_m2 > 0 and p_m2 == 0:
                    prod_dept = "TERMO bo'limi"
                    prod_poly = "ТЕРМО, Чемпион, АЛДОХ"
                elif p_m2 > 0 and t_m2 == 0:
                    prod_dept = "PVX bo'limi"
                    prod_poly = "ПВХ"
                elif p_m2 > 0 and t_m2 > 0:
                    prod_dept = "PVX va TERMO"
                    prod_poly = "ПВХ, ТЕРМО, АЛДОХ"
                else:
                    prod_dept = "PVX bo'limi" if "пвх" in comb_lower else ("TERMO bo'limi" if "терм" in comb_lower else "Sex/Ishlab chiqarish")
                    prod_poly = "ПВХ / ТЕРМО"

                production_summary.append({
                    'emp_id': eid,
                    'id': eid,
                    'name': emp['name'],
                    'position': emp['position'],
                    'dept': prod_dept,
                    'linked_poly': prod_poly,
                    'pvh_m2': p_m2,
                    'termo_m2': t_m2,
                    'fact_m2': total_prod_m2,
                    'tarif_pvh': tarif_pvh,
                    'tarif_termo': tarif_termo,
                    'bonus_pvh': round(p_m2 * tarif_pvh),
                    'bonus_termo': round(t_m2 * tarif_termo),
                    'bonus': prod_bonus,
                    'oklad': emp_oklad,
                    'total_salary': emp_oklad + prod_bonus,
                    'order_count': emp_prod_order_cnt.get(eid, 0),
                    'status_label': 'Ishlab chiqarish bonusi bilan' if total_prod_m2 > 0 else 'Faqat oklad (0 m²)'
                })

            # B) ALOHIDA MONTAJ LAYOUTI (Montaj hajmi borlar yoki montajchilar)
            # Agar bitta xodim ham ishlab chiqaruvchi ham montajchi bo'lsa (masalan Baxtiyor Sayidqulov), u ikkalasida ham chiqadi!
            is_montaj_worker = (
                m_m2 > 0 or 
                "монтаж" in comb_lower
            )
            if is_montaj_worker:
                montaj_summary.append({
                    'emp_id': eid,
                    'id': eid,
                    'name': emp['name'],
                    'position': emp['position'],
                    'dept': "Монтаж",
                    'linked_poly': "АЛДОХ, Чемпион, ТЕРМО, ПВХ",
                    'montaj_m2': m_m2,
                    'fact_m2': m_m2,
                    'tarif': tarif_montaj,
                    'bonus': montaj_bonus,
                    'oklad': emp_oklad,
                    'total_salary': emp_oklad + montaj_bonus,
                    'order_count': emp_mont_order_cnt.get(eid, 0),
                    'status_label': 'Montaj bonusi bilan' if m_m2 > 0 else 'Faqat oklad (0 m²)'
                })

        # 8. HAR BIR XODIMNING REAL-TIME OYLIK VA KPI HISOBI (HAMMA BONUslar BIRLASHADI)
        items_result = []
        s_ok = 0.0
        s_bonus = 0.0
        s_advance = 0.0
        s_total = 0.0
        s_rest = 0.0

        for i, emp in enumerate(employees):
            emp_id = emp['id']
            ms_id = emp.get('ms_id', '')
            oklad = emp['oklad']
            advance = advances_by_emp.get(emp_id, 0.0)

            p_m2 = round(emp_prod_pvh.get(emp_id, 0.0), 2)
            t_m2 = round(emp_prod_termo.get(emp_id, 0.0), 2)
            m_m2 = round(emp_mont_sqm.get(emp_id, 0.0), 2)
            prod_m2 = round(p_m2 + t_m2, 2)

            # 1) Ishlab chiqarish bonusi
            prod_bonus = round(p_m2 * tarif_pvh + t_m2 * tarif_termo)

            # 2) Montaj bonusi
            montaj_bonus = round(m_m2 * tarif_montaj)

            # 3) Sotuv operatori bonusi
            # Agar xodim operatorlar ro'yxatida tanlangan bo'lsa
            is_op_selected = (
                str(emp_id) in selected_op_ids or 
                str(ms_id) in selected_op_ids
            )
            op_info = emp_op_sales.get(emp_id, {'order_count': 0, 'contract_sum': 0.0})
            op_fact = op_info['contract_sum']
            op_plan = ind_plan_op if is_op_selected else 0.0
            op_pct = (op_fact / op_plan * 100.0) if op_plan > 0 else 0.0
            op_bonus = 0.0
            if is_op_selected and op_plan > 0:
                if op_pct >= rules.op_t4:
                    op_bonus = op_fact * (rules.op_k4 / 100.0)
                elif op_pct >= rules.op_t3:
                    op_bonus = op_fact * (rules.op_k3 / 100.0)
                elif op_pct >= rules.op_t2:
                    op_bonus = op_fact * (rules.op_k2 / 100.0)
                elif op_pct >= rules.op_t1:
                    op_bonus = op_fact * (rules.op_k1 / 100.0)
                else:
                    op_bonus = 0.0

            # 4) Sotuvchi / Menejer bonusi
            # Agar xodim sotuvchilar ro'yxatida tanlangan bo'lsa
            is_men_selected = (
                str(emp_id) in selected_men_ids or 
                str(ms_id) in selected_men_ids
            )
            men_info = emp_men_sales.get(emp_id, {'order_count': 0, 'contract_sum': 0.0})
            men_fact = men_info['contract_sum']
            men_plan = ind_plan_men if is_men_selected else 0.0
            men_pct = (men_fact / men_plan * 100.0) if men_plan > 0 else 0.0
            men_bonus = 0.0
            if is_men_selected and men_plan > 0:
                if men_pct >= rules.men_t3:
                    men_bonus = men_fact * (rules.men_r3 / 100.0)
                elif men_pct >= rules.men_t2:
                    men_bonus = men_fact * (rules.men_r2 / 100.0)
                elif men_pct >= rules.men_t1:
                    men_bonus = men_fact * (float(emp.get('manager_rate', 1.0)) / 100.0)
                else:
                    men_bonus = 0.0

            # 5) JAMI KPI BONUSI (Ishlab chiqarish + Montaj + Operator + Sotuvchi)
            total_kpi = round(prod_bonus + montaj_bonus + op_bonus + men_bonus)
            total_salary = round(oklad + total_kpi)
            rest_to_pay = round(total_salary - advance)

            # Asosiy jadvalda fakt va reja ko'rinishi
            if is_men_selected or men_fact > 0:
                main_plan = men_plan
                main_fact = men_fact
                main_pct = men_pct
                main_group = 'men'
            elif is_op_selected or op_fact > 0:
                main_plan = op_plan
                main_fact = op_fact
                main_pct = op_pct
                main_group = 'op'
            elif prod_m2 > 0 or m_m2 > 0 or emp['dept'] in ["PVX bo'limi", "TERMO bo'limi", "Монтаж"]:
                main_plan = 0.0
                main_fact = round(prod_m2 + m_m2, 2)
                main_pct = 100.0 if main_fact > 0 else 0.0
                main_group = 'ish'
            else:
                main_plan = 0.0
                main_fact = 0.0
                main_pct = 0.0
                main_group = 'bosh'

            s_ok += oklad
            s_bonus += total_kpi
            s_advance += advance
            s_total += total_salary
            s_rest += rest_to_pay

            items_result.append({
                'id': emp_id,
                'emp_id': emp_id,
                'ms_id': ms_id,
                'order_num': i,
                'emp_id': emp_id,
                'ms_id': ms_id,
                'name': emp['name'],
                'position': emp['position'],
                'group': main_group,
                'dept': emp['dept'],
                'oklad': oklad,
                'plan': main_plan,
                'fact': main_fact,
                'percent': round(main_pct, 2),
                'kpi_bonus': total_kpi,
                'advance': round(advance),
                'total_salary': total_salary,
                'rest_to_pay': rest_to_pay,
                'is_active': emp['is_active'],

                # Detallashtirilgan KPI ko'rsatkichlari (tooltip va alohida hisob uchun)
                'kpi_prod': prod_bonus,
                'kpi_montaj': montaj_bonus,
                'kpi_op': round(op_bonus),
                'kpi_men': round(men_bonus),
                'pvh_m2': p_m2,
                'termo_m2': t_m2,
                'prod_m2': prod_m2,
                'montaj_m2': m_m2,
                'op_fact': op_fact,
                'op_plan': op_plan,
                'men_fact': men_fact,
                'men_plan': men_plan,
                'is_op_selected': is_op_selected,
                'is_men_selected': is_men_selected,
            })

        return {
            'success': True,
            'is_realtime': True,
            'period': {
                'id': 1,
                'month': month,
                'date_from': str(d_from),
                'date_to': str(d_to),
                'status': 'calculated',
                'status_display': 'Real-Time (MoySklad Jonli)',
                'total_oklad': s_ok,
                'total_kpi': s_bonus,
                'total_advance': s_advance,
                'total_salary': s_total,
                'total_rest': s_rest,
                'moysklad_expense_id': '',
            },
            'rules': {
                'op': {
                    'base': float(rules.op_base),
                    't1': rules.op_t1, 'k1': rules.op_k1,
                    't2': rules.op_t2, 'k2': rules.op_k2,
                    't3': rules.op_t3, 'k3': rules.op_k3,
                    't4': rules.op_t4, 'k4': rules.op_k4,
                },
                'men': {
                    't1': rules.men_t1,
                    't2': rules.men_t2, 'r2': rules.men_r2,
                    't3': rules.men_t3, 'r3': rules.men_r3,
                },
                'ish': {'min': rules.ish_min}
            },
            'total_plans': {
                'op': tot_plan_op,
                'men': tot_plan_men,
                'tarif_pvh': tarif_pvh,
                'tarif_termo': tarif_termo,
                'tarif_montaj': tarif_montaj,
                'op_count': op_count,
                'op_target_count': op_target_count,
                'selected_op_ids': selected_op_ids or [],
                'men_count': men_count,
                'men_target_count': men_target_count,
                'selected_men_ids': selected_men_ids or [],
                'pvh_count': pvh_count,
                'termo_count': termo_count,
                'montaj_count': montaj_count,
            },
            'categories': cats_result,
            'production_summary': production_summary,
            'montaj_summary': montaj_summary,
            'items': items_result,
            'all_employees': [
                {
                    'id': e['id'], 
                    'emp_id': e['id'],
                    'name': e['name'], 
                    'position': e['position'], 
                    'group': e['group'],
                    'dept': e['dept'],
                    'is_active': e['is_active']
                }
                for e in employees if e['is_active']
            ],
            'config': {
                'has_token': bool(self.token),
                'token_preview': f"{self.token[:6]}...{self.token[-4:]}" if self.token and len(self.token) > 10 else "Bor",
                'company_name': "Romax Akfa",
                'last_synced': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
        }
