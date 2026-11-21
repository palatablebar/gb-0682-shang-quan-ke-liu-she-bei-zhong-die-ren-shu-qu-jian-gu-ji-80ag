from app import watermark

def execute(state):
    snap = watermark.snapshot()
    snap['late_in_run'] = watermark.late([c['id'] for c in state.get('calculations', [])], snap['business_high'])
    state['watermark'] = snap
    return state
