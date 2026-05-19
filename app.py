import os
import re
import hmac
import json
import hashlib
import logging
import razorpay
from datetime import datetime, timedelta

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
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY is not set in environment variables")

IS_PRODUCTION = os.getenv('FLASK_ENV') == 'production'

app.config.update(
    DEBUG=os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1', 'yes'),

    # --- Session / Cookie security ---
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=IS_PRODUCTION,   # enforce HTTPS in prod
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2),

    # --- CSRF (flask-wtf) ---
    WTF_CSRF_TIME_LIMIT=3600,              # token valid for 1 hour
    WTF_CSRF_SSL_STRICT=IS_PRODUCTION,

    # --- Razorpay ---
    RAZORPAY_KEY_ID=os.getenv('RAZORPAY_KEY_ID'),
    RAZORPAY_KEY_SECRET=os.getenv('RAZORPAY_KEY_SECRET'),
    RAZORPAY_WEBHOOK_SECRET=os.getenv('RAZORPAY_WEBHOOK_SECRET', ''),
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
    storage_uri='memory://',               # swap to redis:// in prod
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set in environment variables")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
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
# ---------------------------------------------------------------------------

RANKS = {
    'iron':      {'name': 'IRON',      'monthly': 59,   'lifetime': 399},
    'gold':      {'name': 'GOLD',      'monthly': 99,   'lifetime': 699},
    'diamond':   {'name': 'DIAMOND',   'monthly': 179,  'lifetime': 1299},
    'netherite': {'name': 'NETHERITE', 'monthly': 299,  'lifetime': 2199},
    'god':       {'name': 'GOD',       'monthly': 499,  'lifetime': 3599},
}

VALID_BILLING = {'monthly', 'lifetime'}

# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

# Minecraft: 3-16 chars, letters/digits/underscore only
_MC_RE      = re.compile(r'^[A-Za-z0-9_]{3,16}$')
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
    if not _MC_RE.match(mc):
        return False, "Invalid Minecraft username (3-16 chars, letters/digits/underscore)"

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

    # Server-side price check — prevents browser-side tampering
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
    price = rank[billing]

    return render_template(
        'checkout.html',
        rank_name=rank['name'],
        rank_key=rank_key,
        billing=billing,
        price=price,
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
@limiter.limit("10 per minute; 30 per hour")   # per IP — blocks DoS/spam
def create_order():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'error': 'Invalid JSON'}), 400

    is_valid, error = validate_order_input(data)
    if not is_valid:
        logger.warning("Bad create-order input from %s: %s", request.remote_addr, error)
        return jsonify({'success': False, 'error': error}), 400

    # Sanitise before storing
    mc      = data['minecraft_name'].strip()
    discord = data['discord_tag'].strip()
    email   = data['email'].strip().lower()

    amount_paise = int(data['amount']) * 100

    order_data = {
        'amount': amount_paise,
        'currency': 'INR',
        'payment_capture': 1,
        'notes': {
            'minecraft_name': mc,
            'discord_tag':    discord,
            'email':          email,
            'rank':           data['rank'],
            'billing':        data['billing'],
        },
    }

    try:
        order = razorpay_client.order.create(data=order_data)

        record = Payment(
            order_id=order['id'],
            minecraft_name=mc,
            discord_tag=discord,
            email=email,
            rank=data['rank'],
            rank_key=data['rank_key'],
            billing=data['billing'],
            amount=float(data['amount']),
            currency='INR',
            status='pending',
            # payment_id intentionally NULL — filled on verify
        )
        db_session.add(record)
        db_session.commit()

        session['pending_order_id'] = order['id']
        session.permanent = True

        logger.info("Order created: %s for %s", order['id'], email)
        return jsonify({
            'success': True,
            'order_id': order['id'],
            'razorpay_key_id': app.config['RAZORPAY_KEY_ID'],
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
        payment = db_session.query(Payment).filter_by(order_id=order_id).first()
        if not payment:
            return jsonify({'success': False, 'error': 'Order not found'}), 404

        if payment.status == 'completed':
            return jsonify({'success': True})   # idempotent

        payment.payment_id  = payment_id
        payment.status      = 'completed'
        payment.verified_at = datetime.utcnow()
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
    if app.config['RAZORPAY_WEBHOOK_SECRET']:
        if not _verify_webhook_signature(raw_body, received_sig):
            logger.warning("Webhook signature invalid from %s", request.remote_addr)
            return jsonify({'error': 'Invalid signature'}), 400

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return jsonify({'error': 'Bad JSON'}), 400

    event_type = payload.get('event', 'unknown')

    # 2. Idempotency — skip if we already processed this event
    existing = db_session.query(WebhookEvent).filter_by(event_id=event_id).first()
    if existing:
        logger.info("Duplicate webhook event %s — skipping", event_id)
        return jsonify({'status': 'already processed'}), 200

    # 3. Store raw event
    webhook_record = WebhookEvent(
        event_id=event_id or None,
        event_type=event_type,
        payload=raw_body.decode('utf-8'),
        processed='no',
    )
    db_session.add(webhook_record)
    db_session.flush()   # get the record ID without committing yet

    try:
        # 4. Handle known events
        if event_type in ('payment.captured', 'order.paid'):
            _handle_payment_captured(payload)

        elif event_type == 'payment.failed':
            _handle_payment_failed(payload)

        webhook_record.processed = 'yes'
        db_session.commit()
        logger.info("Webhook processed: %s (%s)", event_type, event_id)
        return jsonify({'status': 'ok'}), 200

    except Exception as e:
        db_session.rollback()
        # Re-add the webhook record marked as error so we can investigate
        webhook_record.processed = 'error'
        db_session.add(webhook_record)
        db_session.commit()
        logger.error("Webhook handler error [%s]: %s", event_type, e)
        return jsonify({'status': 'error'}), 500


def _handle_payment_captured(payload: dict):
    """Mark payment as completed when Razorpay confirms capture."""
    entity = (payload.get('payload', {})
                     .get('payment', {})
                     .get('entity', {}))

    razorpay_payment_id = entity.get('id')
    order_id            = entity.get('order_id')

    if not razorpay_payment_id or not order_id:
        logger.warning("payment.captured missing ids: %s", payload)
        return

    payment = db_session.query(Payment).filter_by(order_id=order_id).first()
    if not payment:
        logger.warning("Webhook: order %s not found in DB", order_id)
        return

    if payment.status != 'completed':
        payment.payment_id  = razorpay_payment_id
        payment.status      = 'completed'
        payment.verified_at = datetime.utcnow()
        logger.info("Webhook completed payment: %s", order_id)


def _handle_payment_failed(payload: dict):
    """Mark payment as failed when Razorpay reports failure."""
    entity = (payload.get('payload', {})
                     .get('payment', {})
                     .get('entity', {}))

    order_id = entity.get('order_id')
    if not order_id:
        return

    payment = db_session.query(Payment).filter_by(order_id=order_id).first()
    if payment and payment.status == 'pending':
        payment.status = 'failed'
        logger.info("Webhook marked payment failed: %s", order_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=app.config['DEBUG'])