// api/admin/logs.js
//
// admin.html의 발송 이력 조회 화면이 예전엔 anon 키로 notification_logs를 직접 읽었다.
// users.js와 동일한 패턴으로, 여기서도 requireAdmin으로 검증한 뒤 service_role 키로만 읽는다.

import { requireAdmin } from '../../lib/adminAuth.js';

export default async function handler(req, res) {
  const auth = await requireAdmin(req);
  if (!auth.ok) {
    return res.status(auth.status).json({ success: false, message: auth.message });
  }
  const { supabaseAdmin } = auth;

  if (req.method !== 'GET') {
    return res.status(405).json({ success: false, message: 'Method Not Allowed' });
  }

  const { data, error, count } = await supabaseAdmin
    .from('notification_logs')
    .select('*', { count: 'exact' })
    .order('created_at', { ascending: false })
    .limit(5000);

  if (error) {
    return res.status(500).json({ success: false, message: error.message });
  }

  return res.status(200).json({ success: true, logs: data || [], count });
}
