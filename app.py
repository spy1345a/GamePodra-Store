import os
import hmac
import hashlib
import logging
import razorpay
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, abort
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Secret key — MUST be set in .env, never hardcoded
app.secret_key = os.getenv('FLASK_SECRET_KEY')
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY is not set in environment variables")

app.config.update(
    DEBUG=os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1', 'yes'),
    SESSION_COOKIE_HTTPONLY=True,       # JS cannot read the cookie
    SESSION_COOKIE_SAMESITE='Lax',      # CSRF protection
    SESSION_COOKIE_SECURE=os.getenv('FLASK_ENV') == 'production',  # HTTPS only in prod
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2),
    RAZORPAY_KEY_ID=os.getenv('RAZORPAY_KEY_ID'),
    RAZORPAY_KEY_SECRET=os.getenv('RAZORPAY_KEY_SECRET'),
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
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
    pool_pre_ping=True,        # auto-reconnect on stale connections
    pool_size=5,
    max_overflow=10,
)
db_session = scoped_session(sessionmaker(bind=engine))  # thread-safe sessions

from models import Base, Payment
Base.metadata.create_all(engine)   # creates tables if they don't exist

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
# Helpers
# ---------------------------------------------------------------------------

def validate_checkout_input(data: dict) -> tuple[bool, str]:
    """Returns (is_valid, error_message)."""
    required = ['minecraft_name', 'discord_tag', 'email', 'rank', 'rank_key', 'billing', 'amount']
    for field in required:
        if not data.get(field):
            return False, f"Missing field: {field}"

    if data['rank_key'] not in RANKS:
        return False, "Invalid rank"

    if data['billing'] not in VALID_BILLING:
        return False, "Invalid billing type"

    # Verify amount hasn't been tampered with
    rank     = RANKS[data['rank_key']]
    expected = rank[data['billing']]
    if int(data.get('amount', 0)) != expected:
        return False, "Amount mismatch — possible tampering"

    return True, ""


def verify_razorpay_signature(payment_id: str, order_id: str, signature: str) -> bool:
    """Manually verify Razorpay signature using HMAC-SHA256."""
    secret = app.config['RAZORPAY_KEY_SECRET'].encode('utf-8')
    message = f"{order_id}|{payment_id}".encode('utf-8')
    expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Teardown: return DB session to pool after each request
# ---------------------------------------------------------------------------

@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session.remove()

# ---------------------------------------------------------------------------
# Routes
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


@app.route('/create-order', methods=['POST'])
def create_order():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'error': 'Invalid JSON'}), 400

    is_valid, error = validate_checkout_input(data)
    if not is_valid:
        logger.warning("Invalid create-order input: %s", error)
        return jsonify({'success': False, 'error': error}), 400

    amount_paise = int(data['amount']) * 100  # Razorpay works in paise

    order_data = {
        'amount': amount_paise,
        'currency': 'INR',
        'payment_capture': 1,
        'notes': {
            'minecraft_name': data['minecraft_name'],
            'discord_tag':    data['discord_tag'],
            'email':          data['email'],
            'rank':           data['rank'],
            'billing':        data['billing'],
        }
    }

    try:
        order = razorpay_client.order.create(data=order_data)

        payment_record = Payment(
            order_id=order['id'],
            minecraft_name=data['minecraft_name'],
            discord_tag=data['discord_tag'],
            email=data['email'],
            rank=data['rank'],
            rank_key=data['rank_key'],
            billing=data['billing'],
            amount=float(data['amount']),
            currency='INR',
            status='pending',
            # payment_id is intentionally NULL here — filled in after payment
        )
        db_session.add(payment_record)
        db_session.commit()

        # Store order_id in server-side session for verification later
        session['pending_order_id'] = order['id']
        session.permanent = True

        logger.info("Order created: %s for %s", order['id'], data['email'])

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
def verify_payment():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'error': 'Invalid JSON'}), 400

    razorpay_payment_id = data.get('razorpay_payment_id')
    razorpay_order_id   = data.get('razorpay_order_id')
    razorpay_signature  = data.get('razorpay_signature')

    if not all([razorpay_payment_id, razorpay_order_id, razorpay_signature]):
        return jsonify({'success': False, 'error': 'Missing payment details'}), 400

    # Security: ensure the order_id matches the one we created in this session
    if session.get('pending_order_id') != razorpay_order_id:
        logger.warning("Session/order_id mismatch — possible replay attack: %s", razorpay_order_id)
        return jsonify({'success': False, 'error': 'Order session mismatch'}), 403

    # Verify signature using HMAC (don't rely solely on Razorpay SDK)
    if not verify_razorpay_signature(razorpay_payment_id, razorpay_order_id, razorpay_signature):
        logger.warning("Signature verification failed for order: %s", razorpay_order_id)
        return jsonify({'success': False, 'error': 'Signature verification failed'}), 400

    try:
        payment = db_session.query(Payment).filter_by(order_id=razorpay_order_id).first()
        if not payment:
            return jsonify({'success': False, 'error': 'Order not found'}), 404

        if payment.status == 'completed':
            # Idempotent — already verified, don't error
            return jsonify({'success': True})

        payment.payment_id  = razorpay_payment_id
        payment.status      = 'completed'
        payment.verified_at = datetime.utcnow()
        db_session.commit()

        # Clear the session order after successful verification
        session.pop('pending_order_id', None)

        logger.info("Payment verified: %s → order %s", razorpay_payment_id, razorpay_order_id)
        return jsonify({'success': True})

    except Exception as e:
        db_session.rollback()
        logger.error("Payment verification DB error: %s", e)
        return jsonify({'success': False, 'error': 'Verification failed'}), 500


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


@app.route('/discord-help')
def discord_help():
    return render_template('discord-help.html')


@app.route('/tnc')
def tnc():
    return render_template('tnc.html')


@app.route('/faq')
def faq():
    return render_template('faq.html')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=app.config['DEBUG'])