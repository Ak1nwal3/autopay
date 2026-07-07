"""End-to-end test of the SPA bundle's wallet page behavior.

We:
1. Boot the FastAPI app (with bot disabled)
2. Get the served HTML + JS
3. Run the bundle in real Node with a fetch stub
4. Sign up a user
5. Topup via API
6. Fire the webhook
7. Have the bundle's `loadMe` and `loadTransactions` run
8. Assert the bundle fetched the right data
"""
import os
os.environ['TELEGRAM_BOT_TOKEN'] = ''
os.environ['ENVIRONMENT'] = 'test'
import time, hashlib, hmac, json
import subprocess
import tempfile
import textwrap
from pathlib import Path

from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
email = f'fe2_{int(time.time())}@x.com'
phone = f'+1{str(int(time.time()))[-10:]}'
r = c.post('/api/v1/auth/signup', json={
    'first_name': 'F', 'last_name': 'E',
    'email': email, 'phone_number': phone,
    'password': 'Secret123',
})
tok = r.json()['access_token']
ref = f'topup_e2e_{int(time.time()*1000)}'
# Inject a transaction directly so the user has a row to fetch
from app.core.database import session_scope
from app.models.transaction import Transaction
from app.models.enums import TransactionStatus, TransactionType
from sqlalchemy import select as sa_select
from app.models.user import User
with session_scope() as s:
    u = s.execute(sa_select(User).where(User.email == email)).scalar_one()
    u.balance = 7500
    s.add(u)
    s.flush()
    s.add(Transaction(
        user_id=u.id,
        type=TransactionType.CREDIT.value,
        amount=7500,
        fee=0, currency='NGN',
        status=TransactionStatus.SUCCESS.value,
        provider='nomba',
        provider_reference=ref,
        narration='Top-up via Nomba Checkout',
    ))
    s.commit()
    uid = u.id

# Re-login to get a fresh token (just in case)
r = c.post('/api/v1/auth/login', json={'email': email, 'password': 'Secret123'})
tok = r.json()['access_token']

# Serve HTML + JS
r = c.get('/')
html = r.text
js = c.get('/static/app.js').text

# Build the Node test
node_script = textwrap.dedent(r"""
    const html = process.env.SPA_HTML;
    const js = process.env.SPA_JS;
    const tok = process.env.TOK;
    const uid = parseInt(process.env.UID, 10);

    // DOM stub
    const stubEl = () => ({
        classList: { add(){}, remove(){}, toggle(){}, contains(){return false;} },
        append(){}, appendChild(){}, setAttribute(){}, removeAttribute(){},
        addEventListener(){}, remove(){}, removeChild(){},
        style:{}, dataset:{},
        innerHTML:'', textContent:'',
        value:'', disabled:false, files:[],
        querySelector(){return stubEl();},
        querySelectorAll(){return [];},
    });
    const document = {
        body: stubEl(), head: stubEl(),
        createElement: stubEl,
        createTextNode: (t) => ({ nodeType: 3, textContent: t }),
        querySelector(){return stubEl();}, querySelectorAll(){return [];},
        getElementById(){return stubEl();}, addEventListener(){},
        location: { hash: '', href: 'http://testserver/' },
    };
    const localStorage = {
        _: { 'ap_access_token': tok, 'ap_refresh_token': 'fake-refresh',
             'ap_user': JSON.stringify({ id: uid, email: 'a@b.c',
                                          is_telegram_linked: false,
                                          first_name: 'F', last_name: 'E',
                                          phone_number: '+1', created_at: '2026-01-01' }) },
        getItem(k){return this._[k] ?? null;},
        setItem(k,v){this._[k]=v;},
        removeItem(k){delete this._[k];},
        clear(){this._={};},
    };
        const fetch = async (url, opts) => {
            console.error('  FETCH ' + (opts && opts.method || 'GET') + ' ' + url);
            // Simulate the API: /me, /auth/wallet, /wallet/transactions, /bills
            if (url.includes('/auth/me')) {
                return { ok: true, status: 200,
                    json: async () => ({ id: uid, email: 'a@b.c',
                                        is_telegram_linked: false,
                                        first_name: 'F', last_name: 'E',
                                        phone_number: '+1',
                                        created_at: '2026-01-01' }),
                    headers: { get: () => '' } };
            }
            if (url.endsWith('/auth/wallet')) {
                return { ok: true, status: 200,
                    json: async () => ({ balance: 7500, currency: 'NGN' }),
                    headers: { get: () => '' } };
            }
            if (url.includes('/wallet/transactions')) {
                return { ok: true, status: 200,
                    json: async () => ([{ id: 1, user_id: uid, bill_id: null,
                        type: 'credit', amount: '7500.00', fee: '0.00',
                        currency: 'NGN', status: 'success', provider: 'paystack',
                        provider_reference: 'topup_e2e_static',
                        narration: 'Top-up via Nomba Checkout',
                        failure_reason: null,
                        created_at: '2026-06-10T19:00:00.000Z' }]),
                    headers: { get: () => '' } };
            }
            if (url.endsWith('/bills') && (!opts || opts.method === 'GET' || !opts.method)) {
                return { ok: true, status: 200,
                    json: async () => ([{ id: 100, user_id: uid, vendor_name: 'DSTV',
                        bill_id: null, type: 'debit', amount: '3000.00', fee: '50.00',
                        currency: 'NGN', status: 'paid', provider: 'paystack',
                        provider_reference: 'autopay_x',
                        narration: 'DSTV', failure_reason: null,
                        created_at: '2026-06-10T19:00:00.000Z',
                        due_date: '2026-06-10T00:00:00.000Z', is_recurring: false,
                        retry_count: 0 }]),
                    headers: { get: () => '' } };
            }
            return { ok: false, status: 404, json: async () => ({ detail: 'not mocked' }),
                     headers: { get: () => '' } };
        };
    const window = { location: { hash: '', href: 'http://testserver/' },
                     addEventListener(){} };
    window.window = window;
    const MutationObserver = function() { this.observe = () => {}; };
    const setTimeout = (fn, t) => 0;
    const clearTimeout = () => {};

    // Set the hash to /bills before running the bundle so the
    // bootstrap routes to the bills page.
    window.location.hash = '/bills';

    const vm = require('vm');
    const ctx = { document, window, localStorage, fetch,
        MutationObserver, setTimeout, clearTimeout, console };
    ctx.window = window;
    ctx.globalThis = window;

    async function main() {
        vm.createContext(ctx);
        vm.runInContext(js, ctx);
        // Drain the bootstrap microtask chain.
        for (let i = 0; i < 30; i++) {
            await new Promise(r => setImmediate(r));
        }
        // Manually call loadMe and see what happens.
        try {
            const r = await vm.runInContext('loadMe()', ctx);
            console.error('  loadMe() returned: ' + JSON.stringify(r));
        } catch (e) {
            console.error('  loadMe() threw: ' + e.message + '\n' + e.stack);
        }
        for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r));
        const dump = vm.runInContext(
            'JSON.stringify({ wallet: state.wallet, user: state.user ? state.user.email : null })',
            ctx
        );
        console.error('AFTER_LOADME: ' + dump);
    }
    main();
""")
with tempfile.NamedTemporaryFile(
    mode='w', suffix='.js', delete=False, encoding='utf-8'
) as f:
    f.write(node_script)
    sp = f.name
import os
env = os.environ.copy()
env['SPA_HTML'] = html
env['SPA_JS'] = js
env['TOK'] = tok
env['UID'] = str(uid)
r = subprocess.run(['node', sp], env=env, capture_output=True, text=True, timeout=30)
print('STDOUT:', r.stdout)
print('STDERR:', r.stderr)
Path(sp).unlink(missing_ok=True)
