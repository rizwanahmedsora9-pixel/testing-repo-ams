from ._common import *  # noqa

from app.services.item_definitions import get_payment_methods

from app.services.client_identity import client_reference_filter
from app.services.ledgers import client_material_entry_filter
from sqlalchemy.orm import joinedload
from app.services.booking_cancel_plan import build_client_booking_cancel_plan

# Category bucket used for a rent / service charge that has no master Material
# row of its own (services are charges, not stock, so they carry no category).
# A rent material that still lives in the Material master keeps its own master
# category (e.g. "RENT") — the report must never invent a category for it.
RENTAL_CATEGORY = "Rental / Services"
UNCATEGORIZED = "Uncategorized"


def _ma_money(value):
    """Round a money figure to paisa — the precision every ledger screen shows."""
    return round(float(value or 0), 2)


def _ma_qty(value):
    """Round a quantity to three decimals (bags / pieces / days)."""
    return round(float(value or 0), 3)


def _ma_norm(value):
    """Identity key for a free-text bill line.

    Bill lines store material names as free text, so the same material appears
    as ``RENT STEEL`` on one bill and ``RENT-STEEL`` on the next.  The master
    lookups already normalise this way (``app.services.lookups``); reusing the
    same rule keeps one material from being reported twice.
    """
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _booking_delivered_split(client):
    """Delivered vs undelivered value of each booking line, keyed by item id.

    A booking is one document: the client is charged the full booked value when
    it is taken, then the goods are dispatched in instalments.  The financial
    ledger has to show both sides of that — the customer's *delivered*
    receivable and the balance still owed on undelivered bags — without
    inventing a second charge for the same rupee.

    Quantity delivered is not recomputed here: it comes from the shared
    booking-cancel plan (``remaining = booked - delivered + booked returns``),
    which is the same rule the Direct Sales validation, the material ledger and
    the cancel preview already use.  Only the money is derived, from each
    line's own rate, so the split can never disagree with the booking total.

    ``client`` must be the real ``Client`` row: the plan filters dispatches by
    client code *or* name, and a placeholder with ``code=None`` would match
    every unattributed (cash) entry instead.
    """
    try:
        plan_rows, _, _ = build_client_booking_cancel_plan(client)
    except Exception:
        logging.getLogger(__name__).exception(
            'booking delivered-split plan failed; ledger falls back to document view'
        )
        return {}
    split = {}
    for row in plan_rows:
        item_id = row.get('item_id')
        if item_id is None:
            continue
        try:
            remaining_qty = float(row.get('remaining_qty') or 0)
            rate = float(row.get('rate') or 0)
        except (TypeError, ValueError):
            continue
        split[item_id] = {
            'undelivered_qty': remaining_qty,
            'undelivered_amount': _money_round(Decimal(str(remaining_qty * rate))),
        }
    return split


def _finalise_financial_history(financial_history, parse_dt):
    """Order the rows and stamp every row's running balance (full history).

    Single implementation shared by the initial build and the later append
    pass, so the opening row always leads and the running balance is identical
    no matter which pass produced the rows.
    """
    opening_rows = [row for row in financial_history if row.get('bill_no') == 'OPENING']
    other_rows = [row for row in financial_history if row.get('bill_no') != 'OPENING']
    other_rows.sort(key=lambda r: parse_dt(r.get('date')))
    ordered = opening_rows + other_rows
    running_balance = Decimal('0.00')
    for item in ordered:
        item['debit'] = _money_round(item.get('debit', 0))
        item['credit'] = _money_round(item.get('credit', 0))
        running_balance += Decimal(str(item['debit'])) - Decimal(str(item['credit']))
        bal = running_balance.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if bal == Decimal('-0.00'):
            bal = Decimal('0.00')
        item['balance'] = float(bal)
    return ordered


def build_client_material_analysis(client_name_norm, materials=None, services=None,
                                   client_code=None):
    """Material-wise and category-wise analysis of a client's billed ledger lines.

    Built strictly from the client's own ledger data: the line items of every
    non-void Booking and DirectSale belonging to that client.  No other
    client's rows are ever considered, and a DirectSale is matched by client
    code as well as by the name snapshot so a renamed client cannot lose its
    history.

    Returns ``(material_rows, category_rows, material_grand, category_grand)``:

    * ``material_rows``  — one row per material/service: material, code, unit,
      category, qty, amount, rent, lines, is_rental.
    * ``category_rows``  — one row per category: category, materials, qty,
      payment, rent, share.
    * ``material_grand`` / ``category_grand`` — totals dicts (qty, amount or
      payment, rent, materials, categories, lines).

    Line value is ``qty × rate − line discount`` (the net billed amount, never
    below zero), exactly what the sale form posts.  A line is a rental charge
    when the shared ``is_service_name`` rule says so — the same rule the sale
    form, ``rent_item_revenue`` and the P&L use — so rent is counted the same
    way everywhere in the application.
    """
    from app.services.lookups import is_service_name

    # --- Master indexes -----------------------------------------------------
    # Two lookups are kept per master: an exact (case/space-insensitive) name
    # or code match, then the normalised key the rest of the app resolves
    # names with.  Records are ordered by id so a duplicated master name always
    # resolves the same way on every request.
    if materials is None:
        materials = Material.query.all()
    if services is None:
        services = Service.query.all()

    def _index(records):
        exact, norm = {}, {}
        for rec in sorted(records, key=lambda r: (r.id or 0)):
            for value in (getattr(rec, 'name', None), getattr(rec, 'code', None)):
                if not value:
                    continue
                exact.setdefault(str(value).strip().lower(), rec)
                norm.setdefault(_ma_norm(value), rec)
        return exact, norm

    mat_exact, mat_norm = _index(materials)
    svc_exact, svc_norm = _index(services)

    def _lookup(raw_name):
        """Return ``(kind, record)`` for a bill-line name (service first)."""
        text = (raw_name or '').strip()
        if not text:
            return None, None
        low = text.lower()
        key = _ma_norm(text)
        svc = svc_exact.get(low) or svc_norm.get(key)
        if svc is not None:
            return 'service', svc
        mat = mat_exact.get(low) or mat_norm.get(key)
        if mat is not None:
            return 'material', mat
        return None, None

    def _describe(raw_name):
        """Report attributes + aggregation key for one free-text bill line."""
        kind, rec = _lookup(raw_name)
        if kind == 'service':
            return {
                'key': 'service:%s' % (rec.id,),
                'name': rec.name or raw_name,
                'category': RENTAL_CATEGORY,
                'code': rec.code or '',
                'unit': getattr(rec, 'unit', '') or '',
                'is_rental': True,
            }
        if kind == 'material':
            category = (rec.category.name if rec.category and rec.category.name
                        else UNCATEGORIZED)
            return {
                'key': 'material:%s' % (rec.id,),
                'name': rec.name or raw_name,
                'category': category,
                'code': rec.code or '',
                'unit': rec.unit or '',
                'is_rental': is_service_name(rec.name) or is_service_name(raw_name),
            }
        # Historical / renamed item that is no longer in either master.
        rental = is_service_name(raw_name)
        return {
            'key': 'name:' + (_ma_norm(raw_name) or raw_name.strip().lower()),
            'name': raw_name.strip(),
            'category': RENTAL_CATEGORY if rental else UNCATEGORIZED,
            'code': '',
            'unit': '',
            'is_rental': rental,
        }

    # --- Collect this client's billed lines (bookings + direct sales) -------
    agg = {}

    _describe_cache = {}

    def _describe_cached(name):
        info = _describe_cache.get(name)
        if info is None:
            info = _describe(name)
            _describe_cache[name] = info
        return info

    def _add_line(raw_name, qty, rate, discount):
        name = (raw_name or '').strip()
        if not name:
            return
        info = _describe_cached(name)
        is_rental = info['is_rental']
        qty = float(qty or 0)
        amount = max(0.0, _ma_money(qty * float(rate or 0) - float(discount or 0)))
        entry = agg.get(info['key'])
        if entry is None:
            entry = {
                'name': info['name'],
                'category': info['category'],
                'code': info['code'],
                'unit': info['unit'],
                'is_rental': is_rental,
                'qty': 0.0,
                'amount': 0.0,
                'rent': 0.0,
                'lines': 0,
            }
            agg[info['key']] = entry
        elif is_rental:
            entry['is_rental'] = True
        entry['qty'] += qty
        entry['amount'] += amount
        entry['lines'] += 1
        if is_rental:
            entry['rent'] += amount

    # Bookings are few (an order, not a movement), so they are read normally —
    # with their lines eager-loaded so one query serves the whole set.
    bookings = Booking.query.options(
        selectinload(Booking.items)
    ).filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm,
        Booking.is_void == False,  # noqa: E712
    ).all()
    for b in bookings:
        for item in b.items:
            _add_line(item.material_name, item.qty, item.price_at_time, item.discount)

    # Direct-sale lines are aggregated IN SQL.  A busy client can own tens of
    # thousands of lines; hydrating every SaleItem object just to add qty/amount
    # made this tab the slowest part of the page.  Grouping per material name
    # first keeps the (few) free-text names, so the identity/discount rules in
    # _add_line still see every row it used to see — only in fewer round trips.
    sale_filter = client_reference_filter(
        DirectSale.client_code, DirectSale.client_name, code=client_code, name=client_name_norm,
    )
    line_gross = func.sum(
        DirectSaleItem.qty * func.coalesce(DirectSaleItem.price_at_time, 0)
        - func.coalesce(DirectSaleItem.discount, 0)
    )
    line_rows = db.session.query(
        DirectSaleItem.product_name.label('product_name'),
        func.coalesce(func.sum(DirectSaleItem.qty), 0).label('qty'),
        func.coalesce(line_gross, 0).label('gross'),
        func.count(DirectSaleItem.id).label('lines'),
    ).join(
        DirectSale, DirectSale.id == DirectSaleItem.sale_id
    ).filter(
        sale_filter,
        DirectSale.is_void == False,  # noqa: E712
    ).group_by(DirectSaleItem.product_name).all()
    for row in line_rows:
        name = (row.product_name or '').strip()
        if not name:
            continue
        info = _describe_cached(name)
        is_rental = info['is_rental']
        # Exactly the _add_line arithmetic, applied to the SQL sub-totals:
        # per line it was max(0, round(qty x rate - discount)); summing those
        # line values is the same as taking the gross of the group when every
        # line is non-negative (the only case where max(0, ...) can bite is a
        # discount larger than the line, so the floor is re-applied per group).
        qty_total = float(row.qty or 0)
        amount = max(0.0, _ma_money(float(row.gross or 0)))
        entry = agg.get(info['key'])
        if entry is None:
            entry = {
                'name': info['name'],
                'category': info['category'],
                'code': info['code'],
                'unit': info['unit'],
                'is_rental': is_rental,
                'qty': 0.0,
                'amount': 0.0,
                'rent': 0.0,
                'lines': 0,
            }
            agg[info['key']] = entry
        elif is_rental:
            entry['is_rental'] = True
        entry['qty'] += qty_total
        entry['amount'] += amount
        entry['lines'] += int(row.lines or 0)
        if is_rental:
            entry['rent'] += amount

    # --- Material-wise rows (one row per master material / service) ---------
    material_rows = []
    for entry in agg.values():
        material_rows.append({
            'material': entry['name'],
            'code': entry['code'],
            'unit': entry['unit'],
            'category': entry['category'],
            'qty': _ma_qty(entry['qty']),
            'amount': _ma_money(entry['amount']),
            'rent': _ma_money(entry['rent']),
            'lines': entry['lines'],
            'is_rental': entry['is_rental'],
        })

    def _category_sort(category):
        # Uncategorized always last; everything else alphabetical.
        return (category == UNCATEGORIZED, (category or '').lower())

    material_rows.sort(key=lambda r: (_category_sort(r['category']),
                                      (r['material'] or '').lower()))

    # --- Category-wise rows (subtotals of the material rows) ----------------
    cat_map = {}
    for row in material_rows:
        entry = cat_map.setdefault(row['category'], {
            'category': row['category'],
            'materials': 0,
            'qty': 0.0,
            'payment': 0.0,
            'rent': 0.0,
            'lines': 0,
        })
        entry['materials'] += 1
        entry['qty'] += row['qty']
        entry['payment'] += row['amount']
        entry['rent'] += row['rent']
        entry['lines'] += row['lines']

    category_rows = []
    for entry in cat_map.values():
        entry['qty'] = _ma_qty(entry['qty'])
        entry['payment'] = _ma_money(entry['payment'])
        entry['rent'] = _ma_money(entry['rent'])
        category_rows.append(entry)
    category_rows.sort(key=lambda r: _category_sort(r['category']))

    # --- Grand totals (always the sum of the rows above, never a re-query) --
    material_grand = {
        'qty': _ma_qty(sum(r['qty'] for r in material_rows)),
        'amount': _ma_money(sum(r['amount'] for r in material_rows)),
        'rent': _ma_money(sum(r['rent'] for r in material_rows)),
        'materials': len(material_rows),
        'categories': len(category_rows),
        'lines': sum(r['lines'] for r in material_rows),
    }
    category_grand = {
        'qty': _ma_qty(sum(r['qty'] for r in category_rows)),
        'payment': _ma_money(sum(r['payment'] for r in category_rows)),
        'rent': _ma_money(sum(r['rent'] for r in category_rows)),
        'materials': sum(r['materials'] for r in category_rows),
        'categories': len(category_rows),
        'lines': material_grand['lines'],
    }

    # Share of the billed value (percentage) — presentation only.
    billed = category_grand['payment']
    for row in category_rows:
        row['share'] = round((row['payment'] / billed * 100.0), 1) if billed else 0.0

    return material_rows, category_rows, material_grand, category_grand


@bp.route('/ledger/<int:client_id>')
@login_required
def financial_ledger(client_id):
    client = Client.query.get_or_404(client_id)
    client_name_norm = (client.name or '').strip().lower()

    # --- Server-side paging -------------------------------------------------
    # A busy client can own tens of thousands of movements.  Rendering every
    # row (and an edit modal per row) produced a 265 MB page that took 15 s and
    # could exhaust a small server's RAM.  The ledger is therefore paged on the
    # SERVER, newest first, while every total (opening balance, totals, net
    # balance, per-row running balance and material balances) is still computed
    # over the client's complete history — the arithmetic is unchanged, only
    # the number of <tr> rows sent to the browser is bounded.
    ALL_ROWS = 0  # sentinel: "show everything" (used by print / CSV)

    def _int_arg(name, default):
        try:
            return int(request.args.get(name, default))
        except (TypeError, ValueError):
            return default

    per_page = _int_arg('per_page', 50)
    if per_page <= 0:
        per_page = ALL_ROWS
    else:
        per_page = min(max(per_page, 25), 500)
    page = max(_int_arg('page', 1), 1)
    # Material ledger tab paging (kept independent so switching tabs never
    # moves the other table).
    mat_per_page = _int_arg('mat_per_page', 50)
    if mat_per_page <= 0:
        mat_per_page = ALL_ROWS
    else:
        mat_per_page = min(max(mat_per_page, 25), 500)
    mat_page = max(_int_arg('mat_page', 1), 1)

    def _fmt_dt(dt_val):
        if not dt_val:
            return ''
        if isinstance(dt_val, str):
            return dt_val
        try:
            return dt_val.strftime('%Y-%m-%d %H:%M')
        except Exception:
            return str(dt_val)

    def _parse_dt(dt_val):
        if isinstance(dt_val, datetime):
            return dt_val
        if isinstance(dt_val, date):
            return datetime.combine(dt_val, datetime.min.time())
        if isinstance(dt_val, str):
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                try:
                    return datetime.strptime(dt_val, fmt)
                except ValueError:
                    continue
        return datetime.min

    # 1. Fetch Pending Bills
    pending_bills = PendingBill.query.filter(
        client_reference_filter(PendingBill.client_code, PendingBill.client_name,
                                code=client.code, name=client.name),
        PendingBill.is_void.is_(False),
    ).order_by(PendingBill.id.desc()).all()

    # Sanitize pending bills for template to avoid NoneType error
    for pb in pending_bills:
        if pb.reason is None: pb.reason = ""

    # 2. Financial Ledger
    # Column-limited projections: the financial table only needs these fields,
    # so the page no longer instantiates a full ORM object per movement for a
    # busy client (that alone cost ~20k object builds on a 10k-row history).
    bookings = db.session.query(
        Booking.id, Booking.date_posted, Booking.amount, Booking.paid_amount,
        Booking.discount, Booking.discount_reason, Booking.manual_bill_no,
        Booking.auto_bill_no, Booking.note, Booking.is_void,
    ).filter(func.lower(func.trim(Booking.client_name)) == client_name_norm).all()
    payments = db.session.query(
        Payment.id, Payment.date_posted, Payment.amount, Payment.method,
        Payment.manual_bill_no, Payment.auto_bill_no, Payment.note,
        Payment.is_void, Payment.bank_name, Payment.account_name, Payment.account_no,
        Payment.discount, Payment.discount_reason,
    ).filter(or_(
        Payment.client_id == client.id,
        and_(Payment.client_id.is_(None), func.lower(func.trim(Payment.client_name)) == client_name_norm),
    )).all()
    # Direct sales only need the financial columns here; their lines are
    # aggregated in SQL by the Material Analysis tab when it is opened.
    # ``invoice`` is eager-loaded on the ORM entity because the bill reference
    # falls back to the invoice number (a column list would drop that relation).
    ds_columns = [
        DirectSale.id, DirectSale.date_posted, DirectSale.amount, DirectSale.paid_amount,
        DirectSale.discount, DirectSale.discount_reason, DirectSale.manual_bill_no,
        DirectSale.auto_bill_no, DirectSale.note, DirectSale.is_void,
        DirectSale.rent_variance_loss, DirectSale.invoice_id,
        DirectSale.client_code, DirectSale.category,
    ]
    ds_rows = db.session.query(*ds_columns).filter(
        client_reference_filter(DirectSale.client_code, DirectSale.client_name,
                                code=client.code, name=client.name)
    ).all()
    invoice_nos = {}
    invoice_ids = [row.invoice_id for row in ds_rows if getattr(row, 'invoice_id', None)]
    if invoice_ids:
        for inv_id, inv_no in db.session.query(Invoice.id, Invoice.invoice_no).filter(
            Invoice.id.in_(invoice_ids)
        ).all():
            if inv_no:
                invoice_nos[inv_id] = inv_no
    # A tiny shim gives the row shaper the two relations it reads (``invoice``
    # for the bill number and ``items`` only when the SQL aggregation is not
    # used) without loading a full ORM graph per sale.
    _EmptyInvoiceItems = type('SaleRowShim', (), {})
    direct_sales = []
    for row in ds_rows:
        shim = _EmptyInvoiceItems()
        for col in ds_columns:
            setattr(shim, col.key, getattr(row, col.key))
        inv_no = invoice_nos.get(getattr(row, 'invoice_id', None))
        shim.invoice = type('InvoiceRef', (), {'invoice_no': inv_no})() if inv_no else None
        direct_sales.append(shim)

    # Financial History (Bookings, Payments, Direct Sales) - NO Material Entries
    financial_history = []
    booking_bill_refs = set()
    direct_sale_bill_refs = set()

    cancel_bill_refs = set()
    cancel_amount_by_bill = {}
    cancel_bill_rows = Entry.query.filter(
        client_material_entry_filter(client),
        Entry.type == 'CANCEL',
        Entry.is_void == False
    ).all()
    for ce in cancel_bill_rows:
        bno = (ce.bill_no or '').strip()
        ano = (ce.auto_bill_no or '').strip()
        if bno:
            cancel_bill_refs.add(bno)
        if ano:
            cancel_bill_refs.add(ano)
        bill_ref = bno or ano
        if bill_ref:
            qty = float(ce.qty or 0)
            mat_ref = (ce.material or ce.booked_material or '').strip()
            amount = _resolve_cancel_display_amount(
                client_name_norm=client_name_norm,
                bill_ref=bill_ref,
                mat_ref=mat_ref,
                qty=qty,
                note=getattr(ce, 'note', None)
            )
            if amount is not None and amount > 0:
                cancel_amount_by_bill[bill_ref] = float(cancel_amount_by_bill.get(bill_ref, 0) or 0) + float(amount)

    for b in bookings:
        if b.is_void: continue
        if b.manual_bill_no:
            booking_bill_refs.add(b.manual_bill_no)
        if b.auto_bill_no:
            booking_bill_refs.add(b.auto_bill_no)
        booking_bill_refs.add(f"BK-{b.id}")
        booking_bill_ref = b.manual_bill_no or b.auto_bill_no or f"BK-{b.id}"
        debit = _booking_ledger_gross_due(
            b,
            cancel_value=cancel_amount_by_bill.get(booking_bill_ref, 0),
            allow_legacy_lift=(booking_bill_ref not in cancel_bill_refs)
        )
        credit = b.paid_amount or 0
        discount = getattr(b, 'discount', 0) or 0

        # Delivered / undelivered split -------------------------------------
        # The booking line is the client's single charge; the bags leave in
        # instalments.  Post the *delivered* part as the receivable and hold
        # the undelivered part as a zero-amount note, so the same rupee is
        # never charged twice.  A delivery that nobody has valued yet stays on
        # the document row (previously: the whole booking, as before).
        undelivered_amount = 0.0
        undelivered_qty = 0.0
        delivered_note = ''
        if not discount:
            try:
                booking_items = BookingItem.query.filter_by(booking_id=b.id).order_by(BookingItem.id.asc()).all()
            except Exception:
                booking_items = []
            parts_total = sum(float(getattr(it, 'qty', 0) or 0) * float(getattr(it, 'price_at_time', 0) or 0)
                              for it in booking_items)
            # Only split when the lines explain the document, so a header-level
            # adjustment can never be silently reclassified as delivered.
            if booking_items and abs(parts_total - float(b.amount or 0)) <= 0.01:
                split = _booking_delivered_split(client)
                for it in booking_items:
                    line = split.get(it.id) or {}
                    try:
                        undelivered_qty += float(line.get('undelivered_qty') or 0)
                        undelivered_amount += float(line.get('undelivered_amount') or 0)
                    except (TypeError, ValueError):
                        pass
                if undelivered_amount > 0 and float(b.amount or 0) - undelivered_amount >= 0:
                    delivered_amount = _money_round(Decimal(str(float(b.amount or 0) - undelivered_amount)))
                    if delivered_amount > 0:
                        delivered_note = 'Goods delivered — billed against this booking'
                        debit = delivered_amount
                    else:
                        debit = 0
                        delivered_note = 'No goods delivered yet — whole booking still undelivered'

        financial_history.append({
            'date': b.date_posted,
            'date_display': _fmt_dt(b.date_posted),
            'description': 'Booking',
            'bill_no': booking_bill_ref,
            'note': delivered_note or (b.note or '').strip(),
            'debit': debit,
            'credit': credit,
            'type': 'Booking',
            'id': b.id
        })
        if undelivered_amount > 0:
            # Informational only: the undelivered bags are still owed to the
            # client, but charging them again would double the same rupee.
            # Zero debit/credit keeps every total and running balance intact;
            # the cancel preview (cancel_rows) is what actually writes it off.
            financial_history.append({
                'date': b.date_posted,
                'date_display': _fmt_dt(b.date_posted),
                'description': (
                    'Undelivered balance — '
                    f"{undelivered_qty:.2f}".rstrip('0').rstrip('.') + ' qty'
                ),
                'bill_no': booking_bill_ref,
                'note': (
                    f"Still to be delivered — Rs. {undelivered_amount:,.2f}. "
                    'Held separately; not part of the delivered receivable.'
                ),
                'debit': 0,
                'credit': 0,
                'type': None,
                'id': None,
                'is_undelivered_note': True,
                # Carried for the header tile; never added to debit/credit.
                'undelivered_amount': undelivered_amount
            })
        if float(discount or 0) > 0:
            discount_reason = (getattr(b, 'discount_reason', None) or '').strip()
            discount_desc = 'DISCOUNT WAIVE OFF'
            if discount_reason:
                discount_desc = f'DISCOUNT WAIVE OFF ({discount_reason})'
            financial_history.append({
                'date': b.date_posted,
                'date_display': _fmt_dt(b.date_posted),
                'description': discount_desc,
                'bill_no': booking_bill_ref,
                'note': discount_reason or (b.note or '').strip(),
                'debit': 0,
                'credit': float(discount or 0),
                'type': None,
                'id': None
            })

    waive_rows = WaiveOff.query.filter(
        client_reference_filter(WaiveOff.client_code, WaiveOff.client_name,
                                code=client.code, name=client.name),
        WaiveOff.is_void == False
    ).filter(
        ~func.lower(func.coalesce(WaiveOff.note, '')).like('[direct_sale_discount:%')
    ).order_by(WaiveOff.date_posted.asc(), WaiveOff.id.asc()).all()
    waive_by_payment = {}
    standalone_waive_rows = []
    for w in waive_rows:
        if w.payment_id:
            waive_by_payment.setdefault(w.payment_id, []).append(w)
        else:
            standalone_waive_rows.append(w)

    for p in payments:
        if p.is_void: continue
        amt = p.amount or 0
        method_label = p.method or "Cash"
        pay_details = []
        if getattr(p, 'bank_name', None):
            pay_details.append(f"Bank: {p.bank_name}")
        if getattr(p, 'account_name', None):
            pay_details.append(f"A/C Name: {p.account_name}")
        if getattr(p, 'account_no', None):
            pay_details.append(f"A/C No: {p.account_no}")
        details_suffix = f" - {' | '.join(pay_details)}" if pay_details else ''
        if amt >= 0:
            debit = 0
            credit = amt
            payment_desc = f'Payment ({method_label}){details_suffix}'
        else:
            debit = abs(amt)
            credit = 0
            payment_desc = f'Repayment ({method_label}){details_suffix}'

        # Payment row: only actual cash/bank amount.
        financial_history.append({
            'date': p.date_posted,
            'date_display': _fmt_dt(p.date_posted),
            'description': payment_desc,
            'bill_no': p.manual_bill_no or p.auto_bill_no or f"PAY-{p.id}",
            'note': (p.note or '').strip(),
            'debit': debit,
            'credit': credit,
            'type': 'Payment',
            'id': p.id
        })

        linked_waive_rows = waive_by_payment.get(p.id, [])
        if linked_waive_rows:
            for w in linked_waive_rows:
                w_desc = 'Waive-Off (Loss)'
                if (w.reason or '').strip():
                    w_desc = f'Waive-Off (Loss) ({w.reason.strip()})'
                financial_history.append({
                    'date': w.date_posted or p.date_posted,
                    'date_display': _fmt_dt(w.date_posted or p.date_posted),
                    'description': w_desc,
                    'bill_no': w.bill_no or p.manual_bill_no or p.auto_bill_no or f"PAY-{p.id}",
                    'note': (w.reason or w.note or p.note or '').strip(),
                    'debit': 0,
                    'credit': float(w.amount or 0),
                    'type': None,
                    'id': None
                })
        else:
            # Legacy fallback for older records where waive_off row does not exist.
            p_discount = float(getattr(p, 'discount', 0) or 0)
            if p_discount > 0:
                discount_reason = (getattr(p, 'discount_reason', None) or '').strip()
                discount_desc = 'Waive-Off (Loss)'
                if discount_reason:
                    discount_desc = f'Waive-Off (Loss) ({discount_reason})'
                financial_history.append({
                    'date': p.date_posted,
                    'date_display': _fmt_dt(p.date_posted),
                    'description': discount_desc,
                    'bill_no': p.manual_bill_no or p.auto_bill_no or f"PAY-{p.id}",
                    'note': discount_reason or (p.note or '').strip(),
                    'debit': 0,
                    'credit': p_discount,
                    'type': None,
                    'id': None
                })

    def _waive_bill_ref(row):
        ref = (getattr(row, 'bill_no', None) or '').strip()
        if ref:
            return ref
        marker = (getattr(row, 'note', None) or '').strip()
        m = re.match(r'^\[direct_sale_discount:(\d+)\]$', marker, re.IGNORECASE)
        if m:
            sale = db.session.get(DirectSale, int(m.group(1)))
            if sale:
                return (sale.manual_bill_no or sale.auto_bill_no or f"DS-{sale.id}")
        return ''

    for w in standalone_waive_rows:
        w_desc = 'Waive-Off (Loss)'
        if (w.reason or '').strip():
            w_desc = f'Waive-Off (Loss) ({w.reason.strip()})'
        financial_history.append({
            'date': w.date_posted,
            'date_display': _fmt_dt(w.date_posted),
            'description': w_desc,
            'bill_no': _waive_bill_ref(w),
            'note': (w.reason or w.note or '').strip(),
            'debit': 0,
            'credit': float(w.amount or 0),
            'type': None,
            'id': None
        })

    for s in direct_sales:
        if s.is_void: continue
        sale_bill_ref = (
            (s.invoice.invoice_no if getattr(s, 'invoice', None) else None)
            or s.manual_bill_no
            or s.auto_bill_no
            or f"DS-{s.id}"
        )
        if s.manual_bill_no:
            direct_sale_bill_refs.add(s.manual_bill_no)
        if s.auto_bill_no:
            direct_sale_bill_refs.add(s.auto_bill_no)
        if getattr(s, 'invoice', None) and s.invoice and s.invoice.invoice_no:
            direct_sale_bill_refs.add(s.invoice.invoice_no)
        direct_sale_bill_refs.add(f"UNBILLED-{s.id}")
        direct_sale_bill_refs.add(f"DS-{s.id}")
        direct_sale_bill_refs.add(f"CSH-{s.id}")
        debit = s.amount or 0
        credit = s.paid_amount or 0
        discount = getattr(s, 'discount', 0) or 0
        # A Direct Sale with no financial value is just a dispatch, not a financial event.
        # It should only appear in the material ledger.
        if debit > 0 or credit > 0:
            financial_history.append({
                'date': s.date_posted,
                'date_display': _fmt_dt(s.date_posted),
                'description': 'Direct Sale',
                'bill_no': sale_bill_ref,
                'note': (s.note or '').strip(),
                'debit': debit,
                'credit': credit,
                'type': 'DirectSale',
                'id': s.id
            })
        if float(discount or 0) > 0:
            discount_reason = (getattr(s, 'discount_reason', None) or '').strip()
            discount_desc = 'DISCOUNT WAIVE OFF (Direct Sale)'
            if discount_reason:
                discount_desc = f'DISCOUNT WAIVE OFF (Direct Sale) ({discount_reason})'
            financial_history.append({
                'date': s.date_posted,
                'date_display': _fmt_dt(s.date_posted),
                'description': discount_desc,
                'bill_no': sale_bill_ref,
                'note': discount_reason or (s.note or '').strip(),
                'debit': 0,
                'credit': float(discount or 0),
                'type': None,
                'id': None
            })
        # Company-side delivery rent variance should be visible in client financial timeline
        # as an informational row, but must not alter client running balance.
        rent_loss = float(getattr(s, 'rent_variance_loss', 0) or 0)
        if rent_loss > 0:
            financial_history.append({
                'date': s.date_posted,
                'date_display': _fmt_dt(s.date_posted),
                'description': f'Delivery Rent Variance (Company Loss) Rs.{rent_loss:.2f}',
                'bill_no': sale_bill_ref,
                'note': (s.note or '').strip(),
                'debit': 0,
                'credit': 0,
                'type': None,
                'id': None
            })

    # Explicit booking-cancellation rows for readability in financial ledger.
    cancel_entries = Entry.query.filter(
        client_material_entry_filter(client),
        Entry.type == 'CANCEL',
        Entry.is_void == False
    ).order_by(Entry.date.asc(), Entry.time.asc(), Entry.id.asc()).all()
    for ce in cancel_entries:
        qty = float(ce.qty or 0)
        bill_ref = (ce.bill_no or ce.auto_bill_no or '').strip()
        mat_ref = (ce.material or ce.booked_material or '').strip()
        amount = _resolve_cancel_display_amount(
            client_name_norm=client_name_norm,
            bill_ref=bill_ref,
            mat_ref=mat_ref,
            qty=qty,
            note=getattr(ce, 'note', None)
        )
        desc = f"Booking Cancel ({(ce.material or ce.booked_material or '-')} x {qty:.3f})"
        financial_history.append({
            'date': _parse_ledger_entry_dt(ce.date, ce.time),
            'date_display': _fmt_dt(_parse_ledger_entry_dt(ce.date, ce.time)),
            'description': desc,
            'bill_no': ce.bill_no or '',
            'note': (ce.note or '').strip(),
            'debit': 0,
            'credit': float(amount or 0),
            'type': 'Entry',
            'id': ce.id,
            'is_cancel_entry': True,
            'cancel_amount': amount
        })

    # Sort by date (oldest first)
    opening_balance = _to_float_or_zero(getattr(client, 'opening_balance', 0))
    if opening_balance != 0:
        opening_dt = (
            getattr(client, 'opening_balance_date', None)
            or getattr(client, 'created_at', None)
            or datetime.min
        )
        financial_history.append({
            'date': opening_dt,
            'date_display': _fmt_dt(opening_dt),
            'description': 'Opening Balance',
            'bill_no': 'OPENING',
            'note': (getattr(client, 'opening_balance_note', None) or getattr(client, 'note', None) or '').strip(),
            'debit': opening_balance if opening_balance > 0 else 0,
            'credit': abs(opening_balance) if opening_balance < 0 else 0,
            'type': None,
            'id': None
        })

    opening_rows = [row for row in financial_history if row.get('bill_no') == 'OPENING']
    other_rows = [row for row in financial_history if row.get('bill_no') != 'OPENING']
    other_rows.sort(key=lambda r: _parse_dt(r.get('date')))
    financial_history = opening_rows + other_rows

    # Initial running balance for baseline financial events (Decimal for accuracy).
    running_balance = Decimal('0.00')
    for item in financial_history:
        item['debit'] = _money_round(item.get('debit', 0))
        item['credit'] = _money_round(item.get('credit', 0))
        running_balance += Decimal(str(item['debit'])) - Decimal(str(item['credit']))
        bal = running_balance.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if bal == Decimal('-0.00'):
            bal = Decimal('0.00')
        item['balance'] = float(bal)

    # 3. Material Ledger
    # Booking-reserved material ledger only:
    # - OUT/CANCEL against booked qty
    # - IN only when the return is a booked-material return (not cash/credit stock return)
    deliveries = Entry.query.filter(
        client_material_entry_filter(client),
        or_(
            Entry.type.in_(['OUT', 'CANCEL']),
            and_(
                Entry.type == 'IN',
                Entry.nimbus_no == 'Material Return',
                or_(
                    Entry.client_category == 'Booked Return',
                    Entry.transaction_category == 'Booked Return',
                ),
            )
        )
    ).order_by(Entry.date.asc(), Entry.time.asc()).all()

    material_history = []
    seen_material_bills = set()
    unresolved_dispatches = []

    # Add Bookings to Material Ledger
    bookings = Booking.query.filter(func.lower(func.trim(Booking.client_name)) == client_name_norm).order_by(Booking.date_posted.asc()).all()
    for b in bookings:
        if b.is_void: continue
        for item in b.items:
            created_at = getattr(b, 'created_at', None)
            date_sort = b.date_posted if b.date_posted else None
            if not date_sort and created_at:
                try:
                    date_sort = datetime.strptime(created_at[:19], '%Y-%m-%d %H:%M:%S')
                except Exception:
                    try:
                        date_sort = datetime.strptime(created_at[:10], '%Y-%m-%d')
                    except Exception:
                        date_sort = None
            material_history.append({
                'date': b.date_posted.strftime('%Y-%m-%d') if b.date_posted else (created_at[:10] if created_at else ''),
                'date_sort': date_sort,
                'material': item.material_name,
                'material_group': item.material_name,
                'material_display': item.material_name,
                'qty_added': item.qty,
                'qty_dispatched': 0,
                'bill_no': b.manual_bill_no or b.auto_bill_no or f"BK-{b.id}",
                'nimbus_no': 'Booking',
                'type': 'Booking',
                'note': (b.note or '').strip()
            })

    # Process Deliveries/Entries
    for d in deliveries:
        if d.is_void:
            continue
        bill_ref = d.bill_no or d.auto_bill_no

        if d.type == 'IN' and (d.nimbus_no == 'Material Return'):
            mat_name = d.material or d.booked_material
            if not mat_name:
                continue
            date_sort = None
            try:
                if d.date and d.time:
                    date_sort = datetime.strptime(f"{d.date} {d.time}", '%Y-%m-%d %H:%M:%S')
                elif d.date:
                    date_sort = datetime.strptime(d.date, '%Y-%m-%d')
            except Exception:
                date_sort = None
            material_history.append({
                'date': d.date,
                'date_sort': date_sort,
                'material': mat_name,
                'material_group': mat_name,
                'material_display': mat_name,
                'qty_added': d.qty,
                'qty_dispatched': 0,
                'bill_no': bill_ref,
                'nimbus_no': d.nimbus_no or 'Material Return',
                'type': 'Return',
                'note': (d.note or '').strip()
            })
            continue

        if d.type == 'CANCEL':
            mat_name = d.material or d.booked_material
            if not mat_name:
                continue
            date_sort = None
            try:
                if d.date and d.time:
                    date_sort = datetime.strptime(f"{d.date} {d.time}", '%Y-%m-%d %H:%M:%S')
                elif d.date:
                    date_sort = datetime.strptime(d.date, '%Y-%m-%d')
            except Exception:
                date_sort = None
            material_history.append({
                'date': d.date,
                'date_sort': date_sort,
                'material': mat_name,
                'material_group': mat_name,
                'material_display': mat_name,
                'qty_added': 0,
                'qty_dispatched': d.qty,
                'bill_no': bill_ref,
                'nimbus_no': d.nimbus_no or 'Booking Cancel',
                'type': 'Cancel',
                'note': (d.note or '').strip()
            })
            continue

        is_booking_delivery = (d.client_category == 'Booking Delivery') or (bill_ref in booking_bill_refs)

        if is_booking_delivery:
            group_mat = d.booked_material or d.material
            display_mat = group_mat
            if d.booked_material and d.material and d.booked_material != d.material:
                display_mat = format_alt_material_label(d.booked_material, d.material)
            date_sort = None
            try:
                if d.date and d.time:
                    date_sort = datetime.strptime(f"{d.date} {d.time}", '%Y-%m-%d %H:%M:%S')
                elif d.date:
                    date_sort = datetime.strptime(d.date, '%Y-%m-%d')
            except Exception:
                date_sort = None
            material_history.append({
                'date': d.date,
                'date_sort': date_sort,
                'material': group_mat,
                'material_group': group_mat,
                'material_display': display_mat,
                'qty_added': 0,
                'qty_dispatched': d.qty,
                'bill_no': bill_ref,
                'nimbus_no': d.nimbus_no,
                'type': 'Dispatch',
                'note': (d.note or '').strip()
            })
            if bill_ref:
                seen_material_bills.add(bill_ref)
        else:
            # Material ledger is booking-reserved only.
            # Non-booking dispatches (cash/credit direct sales etc.) must never appear here.
            # They remain visible in financial views/reports as applicable.
            continue

    # Only a booking delivery can contribute material-ledger dispatch rows, and
    # only through its zero-priced (booked) lines.  ``direct_sales`` is a column
    # projection here (its per-line rows are aggregated in SQL for the Material
    # Analysis tab), so the fallback lines are fetched for just the booking
    # deliveries instead of walking a relation that the projection does not carry.
    delivery_item_rows = []
    delivery_sale_ids = [s.id for s in direct_sales
                         if not s.is_void and s.category == 'Booking Delivery']
    if delivery_sale_ids:
        for item_row in db.session.query(
            DirectSaleItem.sale_id, DirectSaleItem.product_name,
            DirectSaleItem.qty, DirectSaleItem.price_at_time
        ).filter(
            DirectSaleItem.sale_id.in_(delivery_sale_ids),
            func.coalesce(DirectSaleItem.price_at_time, 0) <= 0,
        ).order_by(DirectSaleItem.sale_id.asc(), DirectSaleItem.id.asc()).all():
            delivery_item_rows.append(item_row)
    delivery_items_by_sale = {}
    for item_row in delivery_item_rows:
        delivery_items_by_sale.setdefault(item_row.sale_id, []).append(item_row)

    for s in direct_sales:
        if s.is_void: continue
        sale_ref_candidates = set()
        if s.manual_bill_no:
            sale_ref_candidates.add(s.manual_bill_no)
        if s.auto_bill_no:
            sale_ref_candidates.add(s.auto_bill_no)
        if getattr(s, 'invoice', None) and s.invoice and s.invoice.invoice_no:
            sale_ref_candidates.add(s.invoice.invoice_no)

        # If any sale reference is already present from Entry rows, this sale dispatch is
        # already represented and must not be appended again under another bill number.
        if sale_ref_candidates & seen_material_bills:
            continue

        # Also skip standalone Direct Sales in this fallback loop
        if s.category != 'Booking Delivery':
            continue

        bill_ref = (
            (s.invoice.invoice_no if getattr(s, 'invoice', None) and s.invoice else None)
            or s.manual_bill_no
            or s.auto_bill_no
            or f"DS-{s.id}"
        )

        for item in delivery_items_by_sale.get(s.id, []):
            # Only zero-priced (booked) lines reach here; the SQL filter already
            # excluded every priced line of a mixed transaction.

            date_sort = s.date_posted if s.date_posted else None
            material_history.append({
                'date': s.date_posted.strftime('%Y-%m-%d') if s.date_posted else '',
                'date_sort': date_sort,
                'material': item.product_name,
                'material_group': item.product_name,
                'material_display': item.product_name,
                'qty_added': 0,
                'qty_dispatched': item.qty,
                'bill_no': bill_ref,
                'nimbus_no': 'Direct Sale',
                'type': 'Dispatch',
                'note': (s.note or '').strip()
            })

    # Financial rows can also be appended during delivery processing (e.g., non-booking
    # dispatches). Recompute running balances so every rendered row has `balance`.
    financial_history = _finalise_financial_history(financial_history, _parse_dt)

    # Sort by date (oldest first)
    # Sort by date, then by type priority (Booking/Add before Dispatch) to ensure balance doesn't dip
    def mat_sort_key(x):
        d = x.get('date_sort') or datetime.min
        t = x['type']
        # When timestamps exist, sort strictly by time to preserve real order.
        # Only use type priority for rows missing a timestamp.
        if d != datetime.min:
            p = 0
        else:
            if t == 'Booking':
                p = 0
            elif t == 'Cancel':
                p = 1
            elif t == 'Direct Sale':
                p = 2
            else:
                p = 3
        return (d, p)

    material_history.sort(key=mat_sort_key)

    # Running balance per material, over the client's COMPLETE material history.
    # ``material_balance_at`` keeps the balance *before* each row, so a page
    # that starts in the middle of a material's history still shows the true
    # qty added / dispatched; a plain running total would restart at zero on
    # every page and make a paged ledger read like stock appeared from nowhere.
    mat_balances = {}
    material_balance_at = {}
    for item in material_history:
        mat = item.get('material_group') or item['material']
        if mat not in mat_balances:
            mat_balances[mat] = 0
        material_balance_at[id(item)] = mat_balances[mat]
        # Cancellation rows are informational only; do not alter running balance.
        if item.get('type') != 'Cancel':
            mat_balances[mat] += (item.get('qty_added', 0) - item.get('qty_dispatched', 0))
        item['balance'] = mat_balances[mat]

    # Calculate totals — always over the COMPLETE history, before paging.
    total_debit = sum(Decimal(str(item.get('debit', 0))) for item in financial_history)
    total_credit = sum(Decimal(str(item.get('credit', 0))) for item in financial_history)
    total_balance = total_debit - total_credit
    total_debit = _money_round(total_debit)
    total_credit = _money_round(total_credit)
    total_balance = _money_round(total_balance)

    # Undelivered booking value, held apart from the delivered receivable.
    # It is deliberately NOT added to the net balance: the client owes it, but
    # it only becomes a receivable once the bags actually leave the shop.
    undelivered_total = _money_round(Decimal(str(sum(
        float(row.get('undelivered_amount') or 0)
        for row in financial_history
        if row.get('is_undelivered_note')
    ))))
    delivered_balance = _money_round(Decimal(str(total_balance)))

    # ---- Server-side paging of the rendered rows -------------------------
    # Newest movement first (the entry the operator almost always wants); the
    # Opening Balance row is a document line, not a movement, so it stays in the
    # page only when older rows are still being shown.
    financial_total_rows = len(financial_history)
    financial_history_all = financial_history
    if per_page == ALL_ROWS:
        financial_page_rows = financial_history
        financial_page = 1
        financial_pages = 1
    else:
        opening_only = [row for row in financial_history if row.get('bill_no') == 'OPENING']
        movements = [row for row in financial_history if row.get('bill_no') != 'OPENING']
        movements_desc = list(reversed(movements))
        financial_pages = max(1, (len(movements_desc) + per_page - 1) // per_page)
        financial_page = min(page, financial_pages)
        start = (financial_page - 1) * per_page
        financial_page_rows = movements_desc[start:start + per_page]
        if financial_page == financial_pages:
            # last page (i.e. the oldest rows) — keep the opening line visible
            financial_page_rows = opening_only + financial_page_rows
    financial_history = financial_page_rows
    financial_pagination = {
        'page': financial_page,
        'pages': financial_pages,
        'per_page': per_page if per_page != ALL_ROWS else financial_total_rows,
        'total': financial_total_rows,
        'has_prev': financial_page > 1,
        'has_next': financial_page < financial_pages,
        'prev_num': max(1, financial_page - 1),
        'next_num': min(financial_pages, financial_page + 1),
        'show_all': per_page == ALL_ROWS,
        'start': ((financial_page - 1) * per_page + 1) if financial_total_rows else 0,
        'end': min(financial_page * (per_page or financial_total_rows), financial_total_rows),
    }

    # Material ledger tab: same treatment (grouped by material, so the slice is
    # applied to the flat, already-ordered list before grouping).
    material_total_rows = len(material_history)
    if mat_per_page == ALL_ROWS:
        material_page_rows = material_history
        material_page = 1
        material_pages = 1
    else:
        material_pages = max(1, (material_total_rows + mat_per_page - 1) // mat_per_page)
        material_page = min(mat_page, material_pages)
        mstart = (material_page - 1) * mat_per_page
        material_page_rows = material_history[mstart:mstart + mat_per_page]
    material_pagination = {
        'page': material_page,
        'pages': material_pages,
        'per_page': mat_per_page if mat_per_page != ALL_ROWS else material_total_rows,
        'total': material_total_rows,
        'has_prev': material_page > 1,
        'has_next': material_page < material_pages,
        'prev_num': max(1, material_page - 1),
        'next_num': min(material_pages, material_page + 1),
        'show_all': mat_per_page == ALL_ROWS,
    }

    # Group material history by material so UI can render separate sections.
    # Only the current page's rows are grouped; each row's per-material running
    # balance was already stamped over the client's complete list above.
    material_history_grouped = {}
    material_opening_balance = {}
    mat_page_first_row = {}
    for item in material_page_rows:
        mat_name = item.get('material_group') or item.get('material') or 'Unknown'
        material_history_grouped.setdefault(mat_name, []).append(item)
        material_opening_balance.setdefault(mat_name, material_balance_at.get(id(item), 0))
    for mat_name, rows_in_group in material_history_grouped.items():
        if rows_in_group:
            mat_page_first_row[mat_name] = rows_in_group[0]

    # Cancellation preview for remaining bookings (LIFO by booking date).
    # Shared plan: remaining = booked - delivered + returned_booked, so qty
    # credited back by a booked material return is cancellable again.
    from app.services.booking_cancel_plan import build_client_booking_cancel_plan

    plan_rows, cancel_total, cancel_total_qty = build_client_booking_cancel_plan(client)
    cancel_rows = [
        {
            'item_id': row['item_id'],
            'material': row['material'],
            'booking_date': row['booking_date'],
            'bill_no': row['bill_no'],
            'qty_remaining': row['remaining_qty'],
            'rate': row['rate'],
            'amount': row['amount'],
        }
        for row in plan_rows
    ]

    cancel_rows.sort(
        key=lambda x: (x.get('material') or '', x.get('booking_date') or ''),
    )
    cancel_total = _money_round(cancel_total)
    # What cancelling the leftover booking would do to the client's account.
    #
    # The delivered/undelivered split above means ``total_balance`` now holds
    # only the DELIVERED receivable, while ``cancel_total`` values the bags that
    # were never delivered.  Subtracting one from the other would pay the client
    # back money they never handed over (their advance already sits in credit).
    # The pre-split balance is therefore rebuilt by adding the undelivered value
    # back; that is the same figure this preview was computed from before, so
    # the cancel outcome is unchanged.
    cancel_base_balance = Decimal(str(total_balance)) + Decimal(str(undelivered_total or 0))
    cancel_new_balance = _money_round(cancel_base_balance - Decimal(str(cancel_total)))
    cancel_client_due = max(0, cancel_new_balance)
    cancel_company_due = max(0, -cancel_new_balance)

    all_clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    materials = Material.query.order_by(Material.name.asc()).all()
    services = Service.query.order_by(Service.name.asc()).all()

    # Material Analysis tab data (client's billed ledger lines only).
    # OPTIONAL: the tab is behind a click, so it is computed only when
    # ?tab=analysis asks for it (or on request).  The tab button and the tab
    # body both link to this page with tab=analysis, so the panel is there the
    # moment the operator opens it — without charging every ledger view for a
    # second full aggregation.
    analysis_requested = (request.args.get('tab') or '').strip().lower() == 'analysis'
    if analysis_requested:
        material_rows, category_rows, material_grand, category_grand = build_client_material_analysis(
            client_name_norm, materials=materials, services=services,
            client_code=client.code,
        )
    else:
        material_rows, category_rows = [], []
        material_grand = {'qty': 0, 'amount': 0, 'rent': 0, 'materials': 0, 'categories': 0, 'lines': 0}
        category_grand = {'qty': 0, 'payment': 0, 'rent': 0, 'materials': 0, 'categories': 0, 'lines': 0}
    analysis_tab_url = url_for('financial_ledger', client_id=client.id, tab='analysis')
    # The tab is opened by plain JS (it switches panes without a round trip).
    # The server-rendered panel is only a no-JS / fallback view, so it says so
    # instead of showing five zeros that read like "this client bought nothing".
    analysis_status = 'ready' if analysis_requested else 'deferred'

    # Build transaction objects for the edit modals — only for the documents
    # actually rendered on this page (plus everything when printing).
    rendered_ids = set()
    for item in financial_history:
        if item.get('type') and item.get('id') is not None:
            rendered_ids.add(f"{item['type']}{item['id']}")
    show_all_modals = per_page == ALL_ROWS
    transactions_map = {}
    for b in bookings:
        if not b.is_void and (show_all_modals or f"Booking{b.id}" in rendered_ids):
            transactions_map[f"Booking{b.id}"] = b
    for p in payments:
        if not p.is_void and (show_all_modals or f"Payment{p.id}" in rendered_ids):
            transactions_map[f"Payment{p.id}"] = p
    for s in direct_sales:
        if not s.is_void and (show_all_modals or f"DirectSale{s.id}" in rendered_ids):
            transactions_map[f"DirectSale{s.id}"] = s

    return render_template('client_ledger.html',
                           client=client,
                           pending_bills=pending_bills,
                           financial_history=financial_history,
                           material_history=material_history,
                           material_history_grouped=material_history_grouped,
                           unresolved_dispatches=unresolved_dispatches,
                           total_debit=total_debit,
                           total_credit=total_credit,
                           total_balance=total_balance,
                           undelivered_total=undelivered_total,
                           delivered_balance=delivered_balance,
                           cancel_rows=cancel_rows,
                           cancel_total=cancel_total,
                           cancel_total_qty=cancel_total_qty,
                           cancel_new_balance=cancel_new_balance,
                           cancel_client_due=cancel_client_due,
                           cancel_company_due=cancel_company_due,
                           clients=all_clients,
                           materials=materials,
                           material_rows=material_rows,
                           category_rows=category_rows,
                           material_grand=material_grand,
                           category_grand=category_grand,
                           transactions_map=transactions_map,
                           financial_pagination=financial_pagination,
                           material_pagination=material_pagination,
                           financial_total_rows=financial_total_rows,
                           material_total_rows=material_total_rows,
                           opening_balance=opening_balance,
                           material_opening_balance=material_opening_balance,
                           analysis_requested=analysis_requested,
                           analysis_status=analysis_status,
                           analysis_tab_url=analysis_tab_url,
                           payment_methods=get_payment_methods())

