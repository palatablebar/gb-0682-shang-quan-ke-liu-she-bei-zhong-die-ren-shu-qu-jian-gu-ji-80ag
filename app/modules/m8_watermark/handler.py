from flask import Blueprint
from app.web import api,page
bp=Blueprint('m8_watermark',__name__)
@bp.get('/m8_watermark')
def screen():return page(8,'m8_watermark.html')
@bp.route('/api/m8_watermark',methods=['GET','POST'])
def preview():return api(8)
