// api/admin/users.js
//
// admin.html이 예전엔 브라우저에서 anon 키로 users 테이블을 직접 읽고/쓰고 지웠다.
// 이제는 브라우저가 이 서버 API만 호출하고, 실제 DB 접근(service_role 키)은 여기서만 한다.
// 호출자가 진짜 관리자인지는 매 요청마다 requireAdmin()으로 다시 검증한다.

import { requireAdmin } from '../../lib/adminAuth.js';

export default async function handler(req, res) {
  const auth = await requireAdmin(req);
  if (!auth.ok) {
    return res.status(auth.status).json({ success: false, message: auth.message });
  }
  const { supabaseAdmin } = auth;

  if (req.method === 'GET') {
    const { data, error } = await supabaseAdmin
      .from('users')
      .select('*')
      .order('created_at', { ascending: false });

    if (error) {
      return res.status(500).json({ success: false, message: error.message });
    }
    return res.status(200).json({ success: true, users: data || [] });
  }

  if (req.method === 'PATCH') {
    const { email, subscription_status } = req.body || {};
    if (!email || !subscription_status) {
      return res.status(400).json({ success: false, message: 'email과 subscription_status가 필요합니다.' });
    }

    const { error } = await supabaseAdmin
      .from('users')
      .update({ subscription_status, updated_at: new Date().toISOString() })
      .eq('email', email);

    if (error) {
      return res.status(500).json({ success: false, message: error.message });
    }
    return res.status(200).json({ success: true, message: '구독 상태가 변경되었습니다.' });
  }

  if (req.method === 'DELETE') {
    const { confirm } = req.body || {};
    if (confirm !== '전체삭제') {
      return res.status(400).json({ success: false, message: '확인 문구가 일치하지 않습니다.' });
    }

    const { error } = await supabaseAdmin
      .from('users')
      .delete()
      .neq('email', '');

    if (error) {
      return res.status(500).json({ success: false, message: error.message });
    }
    return res.status(200).json({ success: true, message: '전체 회원이 삭제되었습니다.' });
  }

  return res.status(405).json({ success: false, message: 'Method Not Allowed' });
}
