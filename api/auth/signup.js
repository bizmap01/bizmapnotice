// api/auth/signup.js
// 이메일 회원가입 처리. 예전엔 브라우저가 anon 키로 users 테이블에 직접
// insert/upsert 했고 비밀번호는 아예 저장되지 않았다. 지금은 비밀번호를
// bcrypt로 해시해서 저장하고, 성공 시 로그인 유지용 토큰을 발급한다.
// service_role 키는 여기(서버)에서만 쓰이고 브라우저에는 절대 내려가지 않는다.
import { createClient } from '@supabase/supabase-js';
import { hashPassword, issueSessionToken } from '../../lib/emailAuth.js';

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ success: false, message: 'Method Not Allowed' });
  }

  const { name, phone, email, password } = req.body || {};

  if (!name || !name.trim()) {
    return res.status(400).json({ success: false, message: '이름을 입력해 주세요.' });
  }
  if (!phone || !phone.trim()) {
    return res.status(400).json({ success: false, message: '휴대폰 번호를 입력해 주세요.' });
  }
  if (!email || !email.includes('@')) {
    return res.status(400).json({ success: false, message: '올바른 이메일 주소를 입력해 주세요.' });
  }
  if (!password || password.length < 6) {
    return res.status(400).json({ success: false, message: '비밀번호는 최소 6자리 이상이어야 합니다.' });
  }

  const supabaseUrl = process.env.SUPABASE_URL;
  const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!supabaseUrl || !serviceRoleKey) {
    return res.status(500).json({ success: false, message: '서버 환경변수(SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY)가 설정되지 않았습니다.' });
  }

  let payloadEmail;
  try {
    payloadEmail = email.trim().toLowerCase();
  } catch {
    return res.status(400).json({ success: false, message: '올바른 이메일 주소를 입력해 주세요.' });
  }

  const supabaseAdmin = createClient(supabaseUrl, serviceRoleKey, {
    auth: { autoRefreshToken: false, persistSession: false },
  });

  try {
    const { data: existingUser, error: lookupError } = await supabaseAdmin
      .from('users')
      .select('email, password_hash')
      .eq('email', payloadEmail)
      .maybeSingle();

    if (lookupError) {
      return res.status(500).json({ success: false, message: lookupError.message });
    }

    const password_hash = await hashPassword(password);

    if (existingUser) {
      if (existingUser.password_hash) {
        return res.status(409).json({
          success: false,
          message: '이미 가입된 이메일 주소입니다. 로그인해 주세요.',
        });
      }

      // 🔧 [마이그레이션] password_hash가 없는 기존(구버전 방식) 이메일 가입자는
      // 여기서 막지 않고, 지금 입력한 비밀번호로 정식 전환시킨다.
      const { error: updateError } = await supabaseAdmin
        .from('users')
        .update({ name: name.trim(), password_hash })
        .eq('email', payloadEmail);

      if (updateError) {
        return res.status(500).json({ success: false, message: updateError.message });
      }

      const token = issueSessionToken(payloadEmail);
      return res.status(200).json({
        success: true,
        token,
        email: payloadEmail,
        name: name.trim(),
        phone: phone.trim(),
      });
    }

    const { error: insertError } = await supabaseAdmin.from('users').insert([
      {
        email: payloadEmail,
        name: name.trim(),
        password_hash,
        provider: 'email',
        subscription_status: 'inactive',
      },
    ]);

    if (insertError) {
      return res.status(500).json({ success: false, message: insertError.message });
    }

    const token = issueSessionToken(payloadEmail);
    return res.status(200).json({
      success: true,
      token,
      email: payloadEmail,
      name: name.trim(),
      phone: phone.trim(),
    });
  } catch (err) {
    return res.status(500).json({ success: false, message: err.message || '회원가입 처리 중 오류가 발생했습니다.' });
  }
}
