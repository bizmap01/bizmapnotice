// lib/adminAuth.js
//
// admin.html이 예전엔 코드에 박힌 비밀번호 하나로만 보호됐다. 이제는 Supabase Auth로
// 실제 로그인을 하게 하고, 그 로그인한 사람의 이메일이 관리자 허용 목록(ADMIN_EMAILS)에
// 있는지 서버에서 다시 한 번 검증한다. 이 함수를 /api/admin/* 의 모든 엔드포인트 맨 앞에서
// 호출해서, service_role 키로 DB를 직접 만지기 전에 "진짜 관리자가 맞는지"부터 확인한다.

import { createClient } from '@supabase/supabase-js';

export async function requireAdmin(req) {
  const authHeader = req.headers.authorization || '';
  const token = authHeader.startsWith('Bearer ') ? authHeader.slice(7) : null;

  if (!token) {
    return { ok: false, status: 401, message: '로그인 토큰이 없습니다.' };
  }

  const supabaseUrl = process.env.SUPABASE_URL;
  const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!supabaseUrl || !serviceRoleKey) {
    return { ok: false, status: 500, message: 'SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 환경 변수가 설정되지 않았습니다.' };
  }

  const supabaseAdmin = createClient(supabaseUrl, serviceRoleKey);

  const { data, error } = await supabaseAdmin.auth.getUser(token);
  if (error || !data?.user?.email) {
    return { ok: false, status: 401, message: '유효하지 않은 로그인 세션입니다.' };
  }

  const email = data.user.email.toLowerCase();
  const adminEmails = (process.env.ADMIN_EMAILS || '')
    .split(',')
    .map(e => e.trim().toLowerCase())
    .filter(Boolean);

  if (!adminEmails.includes(email)) {
    return { ok: false, status: 403, message: '관리자 권한이 없는 계정입니다.' };
  }

  return { ok: true, supabaseAdmin, email };
}
