from flask import Blueprint, jsonify
from flask_login import login_required
from models import db, Reservation

api_bp = Blueprint('api', __name__, url_prefix='/api')


@api_bp.route('/toggle_arrived/<int:res_id>', methods=['POST'])
@login_required
def toggle_arrived(res_id):
    reservation = Reservation.query.get_or_404(res_id)
    reservation.arrived = not reservation.arrived
    db.session.commit()
    return jsonify({
        'success': True,
        'arrived': reservation.arrived,
        'id': res_id
    })
