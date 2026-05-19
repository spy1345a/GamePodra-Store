import os
import re
import uuid
import hmac
import json
import hashlib
import logging
import razorpay
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template, request, jsonify, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, generate_csrf
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)

app.secret_key = os.getenv('FLASK_SECRET_KEY')
IS_PRODUCTION = os.getenv('FLASK_ENV') == 'production'

app.config.update(
    DEBUG=os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1', 'yes'),

    # --- Session / Cookie security ---
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=IS_PRODUCTION,   # enforce HTTPS in prod
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2),

    # --- Reverse proxy (nginx) ---
    SECURE_PROXY_SSL_HEADER=('X-Forwarded-Proto', 'https'),

    # --- CSRF (flask-wtf) ---
    WTF_CSRF_TIME_LIMIT=3600,              # token valid for 1 hour

    # --- Razorpay ---
    RAZORPAY_KEY_ID=os.getenv('RAZORPAY_KEY_ID'),
    RAZORPAY_KEY_SECRET=os.getenv('RAZORPAY_KEY_SECRET'),
    RAZORPAY_WEBHOOK_SECRET=os.getenv('RAZORPAY_WEBHOOK_SECRET', ''),
)

# ---------------------------------------------------------------------------
# Startup validation — ensure critical config is present
# ---------------------------------------------------------------------------

_REQUIRED_ENV_KEYS = [
    ('FLASK_SECRET_KEY',       'Session signing'),
    ('DATABASE_URL',           'Database connection'),
    ('RAZORPAY_KEY_ID',       'Razorpay API key'),
    ('RAZORPAY_KEY_SECRET',   'Razorpay API secret'),
    ('RAZORPAY_WEBHOOK_SECRET', 'Razorpay webhook verification'),
]

missing_keys = [label for key, label in _REQUIRED_ENV_KEYS if not os.getenv(key)]
if missing_keys:
    raise RuntimeError(
        "Missing required environment variables:\n  - " +
        "\n  - ".join(missing_keys)
    )

# ---------------------------------------------------------------------------
# CSRF protection — covers all state-changing routes automatically
# Exempt only the webhook (signed by Razorpay, not a browser form)
# ---------------------------------------------------------------------------

csrf = CSRFProtect(app)

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],                     # no global limit; set per-route
    storage_uri=os.getenv('RATE_LIMIT_STORAGE', 'memory://'),
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)


def _utcnow():
    """Naive UTC datetime — replacement for deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv('DATABASE_URL')
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,                    # checks connection health before use
    pool_size=5,
    max_overflow=10,
    pool_recycle=1800,                     # FIX: recycle connections every 30 min
                                           # prevents "server has gone away" / random
                                           # 500s after DB idle timeout (common on
                                           # MySQL/MariaDB which default to 8h, and
                                           # some managed Postgres services)
)
db_session = scoped_session(sessionmaker(bind=engine))

from models import Base, Payment, WebhookEvent
Base.metadata.create_all(engine)

# ---------------------------------------------------------------------------
# Razorpay client
# ---------------------------------------------------------------------------

razorpay_client = razorpay.Client(
    auth=(app.config['RAZORPAY_KEY_ID'], app.config['RAZORPAY_KEY_SECRET'])
)

# ---------------------------------------------------------------------------
# Ranks config
# Prices stored in PAISE (integer) — never use floats for money.
# FIX: was storing rupees as float, leading to 59.000000001-style precision bugs
# in the DB and in amount comparisons. All math now stays in integer paise.
# ---------------------------------------------------------------------------

RANKS = {
    'iron':      {'name': 'IRON',      'monthly': 5900,   'lifetime': 39900},
    'gold':      {'name': 'GOLD',      'monthly': 9900,   'lifetime': 69900},
    'diamond':   {'name': 'DIAMOND',   'monthly': 17900,  'lifetime': 129900},
    'netherite': {'name': 'NETHERITE', 'monthly': 29900,  'lifetime': 219900},
    'god':       {'name': 'GOD',       'monthly': 49900,  'lifetime': 359900},
}

VALID_BILLING = {'monthly', 'lifetime'}

# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

# Discord: Username#1234 (legacy) or new @username (2–32 chars)
_DISCORD_RE = re.compile(r'^.{2,37}$')
# Basic email
_EMAIL_RE   = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def validate_order_input(data: dict) -> tuple[bool, str]:
    required = ['minecraft_name', 'discord_tag', 'email',
                'rank', 'rank_key', 'billing', 'amount']
    for field in required:
        if not data.get(field):
            return False, f"Missing field: {field}"

    mc = data['minecraft_name'].strip()
    if not mc:
        return False, "Minecraft name is required"

    discord = data['discord_tag'].strip()
    if not _DISCORD_RE.match(discord):
        return False, "Invalid Discord tag"

    email = data['email'].strip()
    if not _EMAIL_RE.match(email) or len(email) > 255:
        return False, "Invalid email address"

    if data['rank_key'] not in RANKS:
        return False, "Invalid rank"

    if data['billing'] not in VALID_BILLING:
        return False, "Invalid billing type"

    expected = RANKS[data['rank_key']][data['billing']]
    try:
        if int(data['amount']) != expected:
            return False, "Amount mismatch"
    except (ValueError, TypeError):
        return False, "Invalid amount"

    return True, ""


# ---------------------------------------------------------------------------
# Signature helpers
# ---------------------------------------------------------------------------

def _verify_payment_signature(payment_id: str, order_id: str, signature: str) -> bool:
    secret  = app.config['RAZORPAY_KEY_SECRET'].encode()
    message = f"{order_id}|{payment_id}".encode()
    digest  = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


def _verify_webhook_signature(raw_body: bytes, received_sig: str) -> bool:
    secret = app.config['RAZORPAY_WEBHOOK_SECRET'].encode()
    digest = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, received_sig)


def _get_existing_subscription(minecraft_name: str, discord_tag: str) -> dict | None:
    existing = db_session.query(Payment).filter(
        Payment.status == 'completed',
        Payment.minecraft_name == minecraft_name,
        Payment.discord_tag == discord_tag,
    ).order_by(Payment.created_at.desc()).first()

    if not existing:
        return None

    return {
        'id': existing.id,
        'rank_key': existing.rank_key,
        'billing': existing.billing,
        'is_lifetime': existing.is_lifetime,
        'is_expired': existing.is_expired,
        'subscription_end': existing.subscription_end,
        'order_id': existing.order_id,
    }


def _check_expired_subscription(minecraft_name: str, discord_tag: str, existing: dict | None = None) -> tuple[bool, str]:
    if existing is None:
        existing = _get_existing_subscription(minecraft_name, discord_tag)

    if not existing:
        return False, ""

    if existing['billing'] == 'lifetime' and existing['is_lifetime'] and not existing['is_expired']:
        return True, "You already have an active lifetime subscription."

    if existing['is_expired']:
        return False, ""

    if existing['subscription_end'] and existing['subscription_end'] < _utcnow():
        return False, ""

    return True, "You already have an active subscription."


def _can_upgrade(rank_key: str, existing_rank_key: str) -> bool:
    rank_order = ['iron', 'gold', 'diamond', 'netherite', 'god']

    if existing_rank_key not in rank_order or rank_key not in rank_order:
        return False

    existing_index = rank_order.index(existing_rank_key)
    new_index = rank_order.index(rank_key)

    return new_index >= existing_index


def _calculate_upgrade_price(from_rank_key: str, to_rank_key: str, billing: str) -> int:
    if billing != 'lifetime':
        return RANKS[to_rank_key][billing]

    rank_order = ['iron', 'gold', 'diamond', 'netherite', 'god']

    if from_rank_key not in rank_order or to_rank_key not in rank_order:
        return RANKS[to_rank_key]['lifetime']

    from_idx = rank_order.index(from_rank_key)
    to_idx = rank_order.index(to_rank_key)

    if to_idx <= from_idx:
        return RANKS[to_rank_key]['lifetime']

    total = 0
    for i in range(from_idx + 1, to_idx + 1):
        rank = rank_order[i]
        lifetime_price = RANKS[rank]['lifetime']
        prev_lifetime = RANKS[rank_order[i-1]]['lifetime'] if i > 0 else 0
        total += (lifetime_price - prev_lifetime)

    return total if total > 0 else RANKS[to_rank_key]['lifetime']


# ---------------------------------------------------------------------------
# Inject CSRF token into every template context so JS can read it
# ---------------------------------------------------------------------------

@app.context_processor
def inject_csrf_token():
    return {'csrf_token': generate_csrf}


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session.remove()

# ---------------------------------------------------------------------------
# Routes — public pages
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html', ranks=RANKS)


@app.route('/checkout')
def checkout():
    rank_key = request.args.get('rank', 'iron')
    billing  = request.args.get('billing', 'monthly')

    if rank_key not in RANKS:
        rank_key = 'iron'
    if billing not in VALID_BILLING:
        billing = 'monthly'

    rank  = RANKS[rank_key]
    # FIX: price is now in paise; convert to rupees only at the display layer
    price_paise  = rank[billing]
    price_rupees = price_paise // 100

    return render_template(
        'checkout.html',
        rank_name=rank['name'],
        rank_key=rank_key,
        billing=billing,
        price=price_rupees,                # templates show rupees to users
        razorpay_key_id=app.config['RAZORPAY_KEY_ID'],
    )


@app.route('/discord-help')
def discord_help():
    return render_template('discord-help.html')


@app.route('/tnc')
def tnc():
    return render_template('tnc.html')


@app.route('/faq')
def faq():
    return render_template('faq.html')


@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200


@app.route('/payment-success')
def payment_success():
    return render_template(
        'payment-success.html',
        rank_name=request.args.get('rank_name', 'N/A'),
        billing=request.args.get('billing', 'monthly'),
        price=request.args.get('price', '0'),
        payment_id=request.args.get('payment_id', ''),
    )


@app.route('/payment-failed')
def payment_failed():
    return render_template(
        'payment-failed.html',
        rank_name=request.args.get('rank_name', 'N/A'),
        rank_key=request.args.get('rank_key', 'iron'),
        billing=request.args.get('billing', 'monthly'),
        price=request.args.get('price', '0'),
        error=request.args.get('error', 'Payment was not completed'),
    )


# ---------------------------------------------------------------------------
# Routes — payment API  (rate-limited)
# ---------------------------------------------------------------------------

@app.route('/create-order', methods=['POST'])
@limiter.limit("10 per minute; 30 per hour")
def create_order():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'error': 'Invalid JSON'}), 400

    is_valid, error = validate_order_input(data)
    if not is_valid:
        logger.warning("Bad create-order input from %s: %s", request.remote_addr, error)
        return jsonify({'success': False, 'error': error}), 400

    mc = data['minecraft_name'].strip()
    discord = data['discord_tag'].strip()
    email = data['email'].strip().lower()
    rank_key = data['rank_key']
    billing = data['billing']

    existing = _get_existing_subscription(mc, discord)
    is_expired, expired_error = _check_expired_subscription(mc, discord, existing)
    if is_expired:
        return jsonify({'success': False, 'error': expired_error}), 400
    is_upgrade = False
    original_order_id = None

    if existing and not existing['is_expired']:
        if existing['billing'] == 'lifetime' and existing['is_lifetime']:
            return jsonify({'success': False, 'error': 'You already have a lifetime subscription'}), 400

        if billing == 'lifetime':
            if not _can_upgrade(rank_key, existing['rank_key']):
                return jsonify({'success': False, 'error': 'Cannot downgrade rank'}), 400

            is_upgrade = True
            original_order_id = existing['order_id']
            amount_paise = _calculate_upgrade_price(existing['rank_key'], rank_key, billing)

            expected = RANKS[rank_key][billing]
            if amount_paise != expected:
                data['amount'] = str(amount_paise)
        else:
            if not _can_upgrade(rank_key, existing['rank_key']):
                return jsonify({'success': False, 'error': 'Cannot downgrade rank'}), 400
            amount_paise = int(data['amount'])
    else:
        amount_paise = int(data['amount'])

    order_data = {
        'amount': amount_paise,
        'currency': 'INR',
        'payment_capture': 1,
        'notes': {
            'minecraft_name': mc,
            'discord_tag': discord,
            'email': email,
            'rank': data['rank'],
            'billing': billing,
            'is_upgrade': str(is_upgrade).lower(),
            'original_order_id': original_order_id or '',
        },
    }

    try:
        # Lock rows for this user to prevent concurrent duplicate orders
        conflict = db_session.query(Payment).filter(
            Payment.minecraft_name == mc,
            Payment.discord_tag == discord,
            Payment.status.in_(['pending', 'completed']),
        ).with_for_update().first()
        if conflict:
            db_session.rollback()
            logger.warning("Concurrent order blocked for %s/%s", mc, discord)
            return jsonify({'success': False, 'error': 'An order is already being processed for this account'}), 409

        subscription_start = _utcnow()
        if billing == 'lifetime':
            subscription_end = None
            is_lifetime = True
        else:
            subscription_end = subscription_start + timedelta(days=30)
            is_lifetime = False

        # Write Payment record FIRST (with temp order_id) so a DB failure
        # never leaves an orphaned Razorpay order.
        temp_order_id = f'pending_{uuid.uuid4().hex}'
        record = Payment(
            order_id=temp_order_id,
            minecraft_name=mc,
            discord_tag=discord,
            email=email,
            rank=data['rank'],
            rank_key=rank_key,
            billing=billing,
            amount=amount_paise,
            currency='INR',
            status='pending',
            is_lifetime=is_lifetime,
            is_expired=False,
            subscription_start=subscription_start,
            subscription_end=subscription_end,
            original_order_id=original_order_id,
            upgrade_from_monthly=is_upgrade and billing == 'lifetime',
        )
        db_session.add(record)
        db_session.flush()                     # persisted in open transaction

        # Now safe to call Razorpay — if this fails the txn rolls back
        order = razorpay_client.order.create(data=order_data)

        # Update with real Razorpay order_id
        record.order_id = order['id']
        db_session.commit()

        session['pending_order_id'] = order['id']
        session.permanent = True

        logger.info("Order created: %s for %s (upgrade=%s)", order['id'], email, is_upgrade)
        return jsonify({
            'success': True,
            'order_id': order['id'],
            'razorpay_key_id': app.config['RAZORPAY_KEY_ID'],
            'is_upgrade': is_upgrade,
            'upgrade_amount': amount_paise // 100 if is_upgrade else 0,
        })

    except Exception as e:
        db_session.rollback()
        logger.error("Order creation failed: %s", e)
        return jsonify({'success': False, 'error': 'Order creation failed'}), 500


@app.route('/verify-payment', methods=['POST'])
@limiter.limit("10 per minute")
def verify_payment():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'error': 'Invalid JSON'}), 400

    payment_id = data.get('razorpay_payment_id', '').strip()
    order_id   = data.get('razorpay_order_id', '').strip()
    signature  = data.get('razorpay_signature', '').strip()

    if not all([payment_id, order_id, signature]):
        return jsonify({'success': False, 'error': 'Missing payment details'}), 400

    # Replay-attack guard: order must match what we stored in session
    if session.get('pending_order_id') != order_id:
        logger.warning("Session/order mismatch from %s — possible replay: %s",
                       request.remote_addr, order_id)
        return jsonify({'success': False, 'error': 'Order session mismatch'}), 403

    if not _verify_payment_signature(payment_id, order_id, signature):
        logger.warning("Signature failed for order %s from %s", order_id, request.remote_addr)
        return jsonify({'success': False, 'error': 'Signature verification failed'}), 400

    try:
        payment = (db_session.query(Payment)
                             .filter_by(order_id=order_id)
                             .with_for_update()
                             .first())
        if not payment:
            return jsonify({'success': False, 'error': 'Order not found'}), 404

        if payment.status == 'completed':
            return jsonify({'success': True})

        payment.payment_id = payment_id
        payment.status = 'completed'
        payment.verified_at = _utcnow()

        if payment.upgrade_from_monthly and payment.original_order_id:
            old_payment = db_session.query(Payment).filter_by(
                order_id=payment.original_order_id
            ).with_for_update().first()
            if old_payment and old_payment.status == 'completed':
                old_payment.is_lifetime = False
                logger.info("Deactivated old monthly subscription: %s", payment.original_order_id)

        db_session.commit()

        session.pop('pending_order_id', None)
        logger.info("Payment verified: %s → %s", payment_id, order_id)
        return jsonify({'success': True})

    except Exception as e:
        db_session.rollback()
        logger.error("Verify-payment DB error: %s", e)
        return jsonify({'success': False, 'error': 'Verification failed'}), 500


# ---------------------------------------------------------------------------
# Razorpay webhook endpoint
# Handles payment.captured, payment.failed, order.paid events from Razorpay.
# Must be exempted from CSRF (Razorpay signs the payload itself).
# Register this URL in your Razorpay dashboard → Webhooks.
# ---------------------------------------------------------------------------

@app.route('/webhook/razorpay', methods=['POST'])
@csrf.exempt
@limiter.limit("60 per minute")            # generous — Razorpay can burst
def razorpay_webhook():
    raw_body     = request.get_data()
    received_sig = request.headers.get('X-Razorpay-Signature', '')
    event_id     = request.headers.get('X-Razorpay-Event-Id', '')

    # 1. Verify webhook signature
    if not _verify_webhook_signature(raw_body, received_sig):
        logger.warning("Webhook signature invalid from %s", request.remote_addr)
        return jsonify({'error': 'Invalid signature'}), 400

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return jsonify({'error': 'Bad JSON'}), 400

    event_type = payload.get('event', 'unknown')

    # 2. Idempotency — skip if we already processed this event.
    # FIX: race condition — two Razorpay retries landing simultaneously would
    # both pass this check and both call _handle_payment_captured. Fix is to
    # INSERT the WebhookEvent record first with a UNIQUE constraint on event_id
    # and let the DB reject the duplicate, rather than SELECT-then-INSERT.
    # If the insert raises an IntegrityError we know it's a duplicate → 200.
    from sqlalchemy.exc import IntegrityError

    webhook_record = WebhookEvent(
        event_id=event_id or None,
        event_type=event_type,
        payload=raw_body.decode('utf-8'),
        processed='no',
    )
    db_session.add(webhook_record)
    try:
        db_session.flush()   # triggers the UNIQUE constraint if duplicate
    except IntegrityError:
        db_session.rollback()
        logger.info("Duplicate webhook event %s — skipping", event_id)
        return jsonify({'status': 'already processed'}), 200

    try:
        # 3. Handle known events
        # Note: order.paid has different payload structure (payload.order.entity),
        # so it's handled separately below
        if event_type == 'payment.captured':
            _handle_payment_captured(payload)

        elif event_type == 'order.paid':
            _handle_order_paid(payload)

        elif event_type == 'payment.failed':
            _handle_payment_failed(payload)

        webhook_record.processed = 'yes'
        db_session.commit()
        logger.info("Webhook processed: %s (%s)", event_type, event_id)
        return jsonify({'status': 'ok'}), 200

    except Exception as e:
        db_session.rollback()
        # Re-add the webhook record marked as error so we can investigate
        new_error_record = WebhookEvent(
            event_id=None,                 # can't reuse event_id (UNIQUE); store as
                                           # anonymous error record for investigation
            event_type=event_type,
            payload=raw_body.decode('utf-8'),
            processed='error',
        )
        db_session.add(new_error_record)
        db_session.commit()
        logger.error("Webhook handler error [%s]: %s", event_type, e)
        return jsonify({'status': 'error'}), 500


def _handle_payment_captured(payload: dict):
    """Mark payment as completed when Razorpay confirms capture."""
    entity = (payload.get('payload', {})
                     .get('payment', {})
                     .get('entity', {}))

    razorpay_payment_id = entity.get('id')
    order_id = entity.get('order_id')

    if not razorpay_payment_id or not order_id:
        logger.warning("payment.captured missing ids: %s", payload)
        return

    payment = (db_session.query(Payment)
                         .filter_by(order_id=order_id)
                         .with_for_update()
                         .first())
    if not payment:
        logger.warning("Webhook: order %s not found in DB", order_id)
        return

    if payment.status != 'completed':
        payment.payment_id = razorpay_payment_id
        payment.status = 'completed'
        payment.verified_at = _utcnow()

        if payment.upgrade_from_monthly and payment.original_order_id:
            old_payment = db_session.query(Payment).filter_by(
                order_id=payment.original_order_id
            ).with_for_update().first()
            if old_payment and old_payment.status == 'completed':
                old_payment.is_lifetime = False
                logger.info("Webhook: deactivated old monthly subscription: %s", payment.original_order_id)

        logger.info("Webhook completed payment: %s", order_id)


def _handle_payment_failed(payload: dict):
    """Mark payment as failed when Razorpay reports failure."""
    entity = (payload.get('payload', {})
                      .get('payment', {})
                      .get('entity', {}))

    order_id = entity.get('order_id')
    if not order_id:
        return

    # FIX: lock here too — though failure races are lower-risk, be consistent
    payment = (db_session.query(Payment)
                         .filter_by(order_id=order_id)
                         .with_for_update()
                         .first())
    if payment and payment.status == 'pending':
        payment.status = 'failed'
        logger.info("Webhook marked payment failed: %s", order_id)


def _handle_order_paid(payload: dict):
    """Handle order.paid event — just log it; actual completion waits for payment.captured."""
    entity = (payload.get('payload', {})
                      .get('order', {})
                      .get('entity', {}))

    order_id = entity.get('id')
    if not order_id:
        logger.warning("order.paid missing order id: %s", payload)
        return

    logger.info("order.paid received for order %s — awaiting payment.captured", order_id)


# ---------------------------------------------------------------------------
# Entry point
# FIX: app.run() is a single-threaded dev server — one slow DB call or Razorpay
# API call blocks every other request. Use Gunicorn in production:
#
#   gunicorn -w 4 -b 0.0.0.0:5000 app:app
#
# With 4 workers you handle 4 concurrent requests. Add --worker-class gevent
# for async I/O if you're hitting the Razorpay API frequently.
# The app.run() block below is intentionally left for local dev only; it will
# never run under Gunicorn (Gunicorn imports the module, doesn't call __main__).
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=app.config['DEBUG'])