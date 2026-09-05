/* Aquaria back office: authentication, route guard, Settings and the role editor.
 *
 * Separate module from the booking client so the guest journey does not carry the
 * administration screens. Same origin, so `script-src 'self'` is satisfied and no
 * inline handler is needed anywhere.
 *
 * Two rules shape everything here:
 *
 *   1. **The client is not the control.** Every screen this module draws is decided
 *      by permissions the server sent, and every one of those screens re-checks the
 *      same permission on the request that loads its data. Hiding a menu item is a
 *      courtesy; the route guard and the API are the security (§46, §75).
 *   2. **Nothing branches on a role name.** There is no `if (role === 'ADMIN')`
 *      anywhere. `can(page, verb)` reads the effective permission set, so a tenant
 *      that edits a role sees the UI move with it (§48).
 *
 * It reads window.utpApi / utpAuth / utpShowView / utpLang from app.js rather than
 * keeping its own copies: two modules each holding the session token is how one of
 * them ends up using a revoked one.
 */
(function () {
  'use strict';

  const api = (path, options) => window.utpApi(path, options);
  const auth = window.utpAuth;
  const toast = (m, k) => window.utpToast(m, k);
  const showView = (name) => window.utpShowView(name);
  const $ = (id) => document.getElementById(id);
  const lang = () => (window.utpLang ? window.utpLang() : 'en');

  /* ------------------------------------------------------------------ i18n
   *
   * Only the chrome this module invents lives here. Page names, category names,
   * verbs and action names are localized by the server (permission_labels.py) and
   * arrive already translated, so they are never duplicated in this table — one
   * place to translate a permission, and it is the place that also defines it.
   */
  const T = {
    en: {
      login_title: 'Welcome back',
      login_lead: 'Sign in with your staff account to reach the back office.',
      login_email: 'Email', login_password: 'Password',
      login_show: 'Show password', login_hide: 'Hide password',
      login_mfa: 'Authenticator code',
      login_mfa_hint: 'This account requires multi-factor authentication.',
      login_remember: 'Keep me signed in on this device',
      login_forgot: 'Forgot password?', login_submit: 'Sign in',
      login_help: 'Trouble signing in? Contact your administrator.',
      login_forgot_msg: 'Your administrator can send you a new invitation link. Password reset by email is not enabled for this venue yet.',
      reset_title: 'Reset your password', reset_lead: "Enter your email and we'll send a one-time reset code.",
      reset_code: 'Reset code', reset_new_password: 'New password',
      reset_password_hint: 'At least 12 characters, with upper and lower case and a number.',
      reset_request: 'Send reset code', reset_submit: 'Set new password', reset_back: 'Back to sign in',
      reset_code_sent: 'If that email belongs to a staff account, a reset code has been sent.',
      reset_demo_prefix: 'Demo code:', reset_need_all: 'Enter the code and your new password.',
      reset_done: 'Password updated. Sign in with your new password.',
      err_email: 'Please enter your email.',
      err_password: 'Please enter your password.',
      session_expired: 'Your session has expired. Please sign in again.',
      signed_out: 'You have been signed out.',
      need_signin: 'Please sign in to continue.',
      signed_in_as: 'Signed in as {name}',
      bo_menu: 'Menu', bo_logout: 'Sign out', bo_my_access: 'My access',
      scope_org: 'Organization', scope_venue: 'Venue', scope_setting: 'Setting scope',
      scope_level_venue: 'Venue',
      settings_title: 'Settings',
      settings_lead: 'Configure how this venue sells, prices, admits and reports. You see only what your role allows.',
      search_ph: 'Search settings…',
      no_results: 'No settings match that search, or you do not have access to them.',
      recent_used: 'Recently used',
      back_to_settings: 'All settings', back_to_dashboard: 'Back to Settings',
      denied_title: "You don't have permission to access this page.",
      denied_lead: 'Please contact your administrator if you believe you need access.',
      view_only: 'View only', read_only: 'Read only', no_access: 'No access',
      not_applicable: 'Not applicable',
      of_pages: '{shown} of {total} settings available to you',
      pages_available: '{n} settings available',
      protected_note: 'Records on this page are never deleted. {semantics} is what removal does.',
      loading: 'Loading…',
      perms_changed: 'Your access has been updated by an administrator.',
      refresh: 'Reload now',
      cancel: 'Cancel', save_changes: 'Save changes', saved: 'Saved.',
      unsaved: 'You have unsaved changes.',
      discard: 'Discard changes', keep_editing: 'Continue editing',
      confirm_title: 'Confirm change', confirm_from: 'From', confirm_to: 'To',
      confirm_ok: 'Confirm change',
      effective_from: 'Effective from', reason: 'Reason',
      reason_hint: 'Recorded in the audit log with your name.',
      preview: 'Preview', advanced: 'Advanced settings',
      enable: 'Enable', rate: 'Rate',
      mode_included: 'Included', mode_excluded: 'Excluded',
      mode_included_help: 'The selling price already contains this charge.',
      mode_excluded_help: 'This charge is added to the selling price at checkout.',
      price_shown: 'Product price', before_charge: 'Before VAT', charge_amount: 'VAT',
      sc_amount: 'Service charge', grand_total: 'Total',
      history: 'Change history',
      roles_title: 'Roles', roles_lead: 'Choose a role to review or change what it can reach.',
      choose_role: 'Choose a role', expand_all: 'Expand all', collapse_all: 'Collapse all',
      all_view: 'All view', all_add: 'All add', all_edit: 'All edit', all_delete: 'All delete',
      clear_cat: 'Clear', copy_from: 'Copy from role…',
      summary_title: 'Before you save',
      can_view: 'Can view', can_add: 'Can add', can_edit: 'Can edit', can_delete: 'Can delete',
      sensitive: 'Sensitive access', warnings: 'Worth checking',
      save_role: 'Save role',
      eff_title: 'Effective permissions',
      eff_lead: 'The result of combining role, scope and venue — resolved by the same code that enforces every request.',
      timezone: 'Time zone', currency: 'Base currency',
      add_rate: 'Add exchange rate', end_rate: 'End',
      rate_direction: '1 {from} = {rate} {to}',
      no_rates: 'No exchange rates configured.',
      validity_type: 'Default validity', reentry: 'Re-entry allowed', max_entries: 'Maximum entries',
      expires_at: 'Expires at', yes: 'Yes', no: 'No',
      pages_word: 'Pages', actions_word: 'Actions',
      read_only_banner: 'You can view this page but not change it.',
      add_only_banner: 'You can add new records here but not change existing ones.',
      role_saving_disabled: 'Saving role permissions from this screen is not enabled yet. The matrix below is the live registry and reflects what this role holds now.',
      inherited_note: 'This value is inherited from a higher scope. Saving creates an override for this venue.',
      weekday: 'Day', closed: 'Closed', open: 'Open', close: 'Close', last_admission: 'Last admission',
      day_mon: 'Monday', day_tue: 'Tuesday', day_wed: 'Wednesday', day_thu: 'Thursday',
      day_fri: 'Friday', day_sat: 'Saturday', day_sun: 'Sunday',
      enabled_languages: 'Enabled languages', default_language: 'Default language',
      prefix: 'Prefix', padding: 'Number padding',
      secret: 'Secret', secret_on_file: 'A secret is on file, ending', secret_none: 'No secret set yet.',
      secret_leave_blank: 'Leave blank to keep the current secret',
      endpoint: 'Endpoint URL', events: 'Events', name: 'Name', code: 'Code',
      scope_write: 'Allow write access', active: 'Active', add: 'Add', none_yet: 'None configured yet.',
      discount_pct: 'Discount %', commission_pct: 'Commission %',
      no_records: 'Nothing configured here yet.', records_word: 'records',
      records_managed_elsewhere: 'Records are created and edited from their own module.',
      edit: 'Edit', delete: 'Delete', save: 'Save',
      status_word: 'Status', status_active: 'Active', status_inactive: 'Inactive',
      reason_word: 'Reason',
      display_name_word: 'Display name', code_word: 'Code', provider_word: 'Provider', channels_word: 'Channels',
      help_display_name: 'Shown to customers during payment.',
      help_code: 'Internal identifier used by the system and integrations.',
      access_word: 'Access', assign_role: 'Assign role', scope_word: 'Scope', all_venues: 'All venues',
      effective_permissions: 'Effective permissions', no_access_anywhere: 'This staff member has no access at any venue yet.',
      rounding_method: 'Rounding method', rounding_increment: 'Rounding increment',
      round_up_label: 'Always round up', round_up_help: 'Always round the amount upward.',
      round_down_label: 'Always round down', round_down_help: 'Always round the amount downward.',
      round_half_label: 'Standard mathematical rounding', round_half_help: 'Below half rounds down; half or above rounds up.',
      round_none_label: 'No rounding', round_none_help: 'Charge the exact calculated amount.',
      round_original: 'Original amount', round_rounded: 'Rounded amount',
    },
    th: {
      login_title: 'ยินดีต้อนรับกลับ',
      login_lead: 'เข้าสู่ระบบด้วยบัญชีพนักงานเพื่อเข้าใช้ระบบหลังบ้าน',
      login_email: 'อีเมล', login_password: 'รหัสผ่าน',
      login_show: 'แสดงรหัสผ่าน', login_hide: 'ซ่อนรหัสผ่าน',
      login_mfa: 'รหัสยืนยันตัวตน',
      login_mfa_hint: 'บัญชีนี้ต้องยืนยันตัวตนแบบหลายขั้นตอน',
      login_remember: 'ให้ฉันอยู่ในระบบบนอุปกรณ์นี้',
      login_forgot: 'ลืมรหัสผ่าน?', login_submit: 'เข้าสู่ระบบ',
      login_help: 'เข้าสู่ระบบไม่ได้? กรุณาติดต่อผู้ดูแลระบบ',
      login_forgot_msg: 'ผู้ดูแลระบบสามารถส่งลิงก์เชิญใหม่ให้คุณได้ ขณะนี้ยังไม่เปิดใช้การรีเซ็ตรหัสผ่านทางอีเมลสำหรับสถานที่นี้',
      reset_title: 'รีเซ็ตรหัสผ่าน', reset_lead: 'กรอกอีเมลของคุณ แล้วเราจะส่งรหัสรีเซ็ตแบบใช้ครั้งเดียว',
      reset_code: 'รหัสรีเซ็ต', reset_new_password: 'รหัสผ่านใหม่',
      reset_password_hint: 'อย่างน้อย 12 ตัวอักษร มีตัวพิมพ์ใหญ่ พิมพ์เล็ก และตัวเลข',
      reset_request: 'ส่งรหัสรีเซ็ต', reset_submit: 'ตั้งรหัสผ่านใหม่', reset_back: 'กลับไปหน้าเข้าสู่ระบบ',
      reset_code_sent: 'หากอีเมลนี้เป็นบัญชีพนักงาน ระบบได้ส่งรหัสรีเซ็ตแล้ว',
      reset_demo_prefix: 'รหัสสำหรับเดโม:', reset_need_all: 'กรอกรหัสและรหัสผ่านใหม่ของคุณ',
      reset_done: 'อัปเดตรหัสผ่านแล้ว เข้าสู่ระบบด้วยรหัสผ่านใหม่',
      err_email: 'กรุณากรอกอีเมล',
      err_password: 'กรุณากรอกรหัสผ่าน',
      session_expired: 'เซสชันหมดอายุแล้ว กรุณาเข้าสู่ระบบอีกครั้ง',
      signed_out: 'ออกจากระบบแล้ว',
      need_signin: 'กรุณาเข้าสู่ระบบเพื่อดำเนินการต่อ',
      signed_in_as: 'เข้าสู่ระบบในชื่อ {name}',
      bo_menu: 'เมนู', bo_logout: 'ออกจากระบบ', bo_my_access: 'สิทธิ์ของฉัน',
      scope_org: 'องค์กร', scope_venue: 'สถานที่', scope_setting: 'ขอบเขตการตั้งค่า',
      scope_level_venue: 'ระดับสถานที่',
      settings_title: 'การตั้งค่า',
      settings_lead: 'กำหนดวิธีการขาย ราคา การเข้าชม และรายงานของสถานที่นี้ คุณจะเห็นเฉพาะส่วนที่บทบาทของคุณอนุญาต',
      search_ph: 'ค้นหาการตั้งค่า…',
      no_results: 'ไม่พบการตั้งค่าที่ตรงกับคำค้นหา หรือคุณไม่มีสิทธิ์เข้าถึง',
      recent_used: 'ใช้งานล่าสุด',
      back_to_settings: 'การตั้งค่าทั้งหมด', back_to_dashboard: 'กลับไปหน้าการตั้งค่า',
      denied_title: 'คุณไม่มีสิทธิ์เข้าถึงหน้านี้',
      denied_lead: 'หากคุณคิดว่าควรมีสิทธิ์เข้าถึง กรุณาติดต่อผู้ดูแลระบบ',
      view_only: 'ดูได้เท่านั้น', read_only: 'อ่านได้เท่านั้น', no_access: 'ไม่มีสิทธิ์',
      not_applicable: 'ไม่เกี่ยวข้อง',
      of_pages: 'ใช้ได้ {shown} จาก {total} รายการ',
      pages_available: 'ใช้ได้ {n} รายการ',
      protected_note: 'ข้อมูลในหน้านี้จะไม่ถูกลบออกจากระบบ การลบหมายถึง {semantics}',
      loading: 'กำลังโหลด…',
      perms_changed: 'ผู้ดูแลระบบได้ปรับสิทธิ์ของคุณแล้ว',
      refresh: 'โหลดใหม่ตอนนี้',
      cancel: 'ยกเลิก', save_changes: 'บันทึกการเปลี่ยนแปลง', saved: 'บันทึกแล้ว',
      unsaved: 'คุณมีการเปลี่ยนแปลงที่ยังไม่ได้บันทึก',
      discard: 'ละทิ้งการเปลี่ยนแปลง', keep_editing: 'แก้ไขต่อ',
      confirm_title: 'ยืนยันการเปลี่ยนแปลง', confirm_from: 'จาก', confirm_to: 'เป็น',
      confirm_ok: 'ยืนยันการเปลี่ยนแปลง',
      effective_from: 'มีผลตั้งแต่', reason: 'เหตุผล',
      reason_hint: 'จะถูกบันทึกในบันทึกการตรวจสอบพร้อมชื่อของคุณ',
      preview: 'ตัวอย่างการคำนวณ', advanced: 'การตั้งค่าขั้นสูง',
      enable: 'เปิดใช้งาน', rate: 'อัตรา',
      mode_included: 'รวมในราคา', mode_excluded: 'ไม่รวมในราคา',
      mode_included_help: 'ราคาขายที่แสดงได้รวมค่านี้ไว้แล้ว',
      mode_excluded_help: 'ค่านี้จะถูกบวกเพิ่มจากราคาขายเมื่อชำระเงิน',
      price_shown: 'ราคาสินค้า', before_charge: 'ราคาก่อนภาษี', charge_amount: 'ภาษีมูลค่าเพิ่ม',
      sc_amount: 'ค่าบริการ', grand_total: 'รวมทั้งสิ้น',
      history: 'ประวัติการเปลี่ยนแปลง',
      roles_title: 'บทบาท', roles_lead: 'เลือกบทบาทเพื่อตรวจสอบหรือแก้ไขสิ่งที่บทบาทนั้นเข้าถึงได้',
      choose_role: 'เลือกบทบาท', expand_all: 'ขยายทั้งหมด', collapse_all: 'ย่อทั้งหมด',
      all_view: 'ดูทั้งหมด', all_add: 'เพิ่มทั้งหมด', all_edit: 'แก้ไขทั้งหมด', all_delete: 'ลบทั้งหมด',
      clear_cat: 'ล้าง', copy_from: 'คัดลอกจากบทบาท…',
      summary_title: 'ก่อนบันทึก',
      can_view: 'ดูได้', can_add: 'เพิ่มได้', can_edit: 'แก้ไขได้', can_delete: 'ลบได้',
      sensitive: 'สิทธิ์ที่มีความสำคัญสูง', warnings: 'ควรตรวจสอบ',
      save_role: 'บันทึกบทบาท',
      eff_title: 'สิทธิ์ที่มีผลจริง',
      eff_lead: 'ผลลัพธ์จากการรวมบทบาท ขอบเขต และสถานที่ คำนวณด้วยชุดคำสั่งเดียวกับที่บังคับใช้ในทุกคำขอ',
      timezone: 'เขตเวลา', currency: 'สกุลเงินหลัก',
      add_rate: 'เพิ่มอัตราแลกเปลี่ยน', end_rate: 'สิ้นสุด',
      rate_direction: '1 {from} = {rate} {to}',
      no_rates: 'ยังไม่ได้ตั้งค่าอัตราแลกเปลี่ยน',
      validity_type: 'อายุการใช้งานเริ่มต้น', reentry: 'อนุญาตให้เข้าซ้ำ', max_entries: 'จำนวนครั้งเข้าสูงสุด',
      expires_at: 'หมดอายุเวลา', yes: 'ใช่', no: 'ไม่',
      pages_word: 'หน้า', actions_word: 'การดำเนินการ',
      read_only_banner: 'คุณดูหน้านี้ได้ แต่ไม่สามารถแก้ไขได้',
      add_only_banner: 'คุณเพิ่มรายการใหม่ได้ แต่ไม่สามารถแก้ไขรายการที่มีอยู่ได้',
      role_saving_disabled: 'ยังไม่เปิดให้บันทึกสิทธิ์ของบทบาทจากหน้าจอนี้ ตารางด้านล่างคือทะเบียนสิทธิ์จริงและแสดงสิทธิ์ที่บทบาทนี้มีอยู่ในปัจจุบัน',
      inherited_note: 'ค่านี้สืบทอดมาจากขอบเขตที่สูงกว่า การบันทึกจะสร้างค่าเฉพาะสำหรับสถานที่นี้',
      weekday: 'วัน', closed: 'ปิด', open: 'เปิด', close: 'ปิดทำการ', last_admission: 'เข้าชมรอบสุดท้าย',
      day_mon: 'จันทร์', day_tue: 'อังคาร', day_wed: 'พุธ', day_thu: 'พฤหัสบดี',
      day_fri: 'ศุกร์', day_sat: 'เสาร์', day_sun: 'อาทิตย์',
      enabled_languages: 'ภาษาที่เปิดใช้งาน', default_language: 'ภาษาเริ่มต้น',
      prefix: 'คำนำหน้า', padding: 'จำนวนหลัก',
      secret: 'รหัสลับ', secret_on_file: 'มีรหัสลับบันทึกไว้ ลงท้ายด้วย', secret_none: 'ยังไม่ได้ตั้งรหัสลับ',
      secret_leave_blank: 'เว้นว่างไว้เพื่อใช้รหัสลับเดิม',
      endpoint: 'URL ปลายทาง', events: 'เหตุการณ์', name: 'ชื่อ', code: 'รหัส',
      scope_write: 'อนุญาตให้เขียนข้อมูล', active: 'ใช้งาน', add: 'เพิ่ม', none_yet: 'ยังไม่มีการตั้งค่า',
      discount_pct: 'ส่วนลด %', commission_pct: 'ค่าคอมมิชชัน %',
      no_records: 'ยังไม่มีการตั้งค่าในหน้านี้', records_word: 'รายการ',
      records_managed_elsewhere: 'รายการถูกสร้างและแก้ไขจากโมดูลของตนเอง',
      edit: 'แก้ไข', delete: 'ลบ', save: 'บันทึก',
      status_word: 'สถานะ', status_active: 'ใช้งาน', status_inactive: 'ปิดใช้งาน',
      reason_word: 'เหตุผล',
      display_name_word: 'ชื่อที่แสดง', code_word: 'รหัส', provider_word: 'ผู้ให้บริการ', channels_word: 'ช่องทาง',
      help_display_name: 'แสดงต่อลูกค้าระหว่างการชำระเงิน',
      help_code: 'ตัวระบุภายในที่ระบบและการเชื่อมต่อใช้งาน',
      access_word: 'สิทธิ์เข้าถึง', assign_role: 'กำหนดบทบาท', scope_word: 'ขอบเขต', all_venues: 'ทุกสาขา',
      effective_permissions: 'สิทธิ์ที่มีผล', no_access_anywhere: 'พนักงานคนนี้ยังไม่มีสิทธิ์เข้าถึงสาขาใด',
      rounding_method: 'วิธีปัดเศษ', rounding_increment: 'ขั้นการปัดเศษ',
      round_up_label: 'ปัดขึ้นเสมอ', round_up_help: 'ปัดจำนวนเงินขึ้นเสมอ',
      round_down_label: 'ปัดลงเสมอ', round_down_help: 'ปัดจำนวนเงินลงเสมอ',
      round_half_label: 'ปัดเศษแบบคณิตศาสตร์', round_half_help: 'ต่ำกว่าครึ่งปัดลง ตั้งแต่ครึ่งขึ้นไปปัดขึ้น',
      round_none_label: 'ไม่ปัดเศษ', round_none_help: 'คิดตามจำนวนที่คำนวณได้จริง',
      round_original: 'จำนวนเดิม', round_rounded: 'จำนวนหลังปัด',
    },
    zh: {
      login_title: '欢迎回来',
      login_lead: '请使用员工账号登录后台。',
      login_email: '邮箱', login_password: '密码',
      login_show: '显示密码', login_hide: '隐藏密码',
      login_mfa: '验证器代码',
      login_mfa_hint: '此账号需要多重身份验证。',
      login_remember: '在此设备上保持登录',
      login_forgot: '忘记密码？', login_submit: '登录',
      login_help: '无法登录？请联系管理员。',
      login_forgot_msg: '管理员可以为您重新发送邀请链接。本场馆尚未启用邮件重置密码。',
      reset_title: '重置密码', reset_lead: '输入您的邮箱，我们将发送一次性重置码。',
      reset_code: '重置码', reset_new_password: '新密码',
      reset_password_hint: '至少 12 个字符，包含大小写字母和数字。',
      reset_request: '发送重置码', reset_submit: '设置新密码', reset_back: '返回登录',
      reset_code_sent: '如果该邮箱属于员工账号，已发送重置码。',
      reset_demo_prefix: '演示码：', reset_need_all: '请输入重置码和新密码。',
      reset_done: '密码已更新，请使用新密码登录。',
      err_email: '请输入邮箱。',
      err_password: '请输入密码。',
      session_expired: '会话已过期，请重新登录。',
      signed_out: '您已退出登录。',
      need_signin: '请先登录后继续。',
      signed_in_as: '已登录：{name}',
      bo_menu: '菜单', bo_logout: '退出登录', bo_my_access: '我的权限',
      scope_org: '组织', scope_venue: '场馆', scope_setting: '设置范围',
      scope_level_venue: '场馆级',
      settings_title: '设置',
      settings_lead: '配置本场馆的销售、定价、入场与报表方式。您只能看到角色允许的部分。',
      search_ph: '搜索设置…',
      no_results: '没有匹配的设置，或您没有访问权限。',
      recent_used: '最近使用',
      back_to_settings: '全部设置', back_to_dashboard: '返回设置',
      denied_title: '您没有权限访问此页面。',
      denied_lead: '如果您认为需要访问权限，请联系管理员。',
      view_only: '仅可查看', read_only: '只读', no_access: '无权限',
      not_applicable: '不适用',
      of_pages: '可用 {shown} / {total} 项设置',
      pages_available: '可用 {n} 项设置',
      protected_note: '本页记录不会被删除。删除即代表 {semantics}。',
      loading: '加载中…',
      perms_changed: '管理员已更新您的权限。',
      refresh: '立即重新加载',
      cancel: '取消', save_changes: '保存更改', saved: '已保存。',
      unsaved: '您有未保存的更改。',
      discard: '放弃更改', keep_editing: '继续编辑',
      confirm_title: '确认更改', confirm_from: '原值', confirm_to: '新值',
      confirm_ok: '确认更改',
      effective_from: '生效日期', reason: '原因',
      reason_hint: '将连同您的姓名记入审计日志。',
      preview: '计算预览', advanced: '高级设置',
      enable: '启用', rate: '税率',
      mode_included: '价内', mode_excluded: '价外',
      mode_included_help: '所显示的售价已包含该费用。',
      mode_excluded_help: '结账时该费用将在售价之外另行计收。',
      price_shown: '商品价格', before_charge: '税前金额', charge_amount: '增值税',
      sc_amount: '服务费', grand_total: '合计',
      history: '变更记录',
      roles_title: '角色', roles_lead: '选择一个角色以查看或修改其可访问的范围。',
      choose_role: '选择角色', expand_all: '全部展开', collapse_all: '全部收起',
      all_view: '全选查看', all_add: '全选新增', all_edit: '全选编辑', all_delete: '全选删除',
      clear_cat: '清空', copy_from: '从角色复制…',
      summary_title: '保存前确认',
      can_view: '可查看', can_add: '可新增', can_edit: '可编辑', can_delete: '可删除',
      sensitive: '敏感权限', warnings: '建议核对',
      save_role: '保存角色',
      eff_title: '有效权限',
      eff_lead: '角色、范围与场馆组合后的结果，由执行每个请求的同一段代码解析得出。',
      timezone: '时区', currency: '基础货币',
      add_rate: '新增汇率', end_rate: '结束',
      rate_direction: '1 {from} = {rate} {to}',
      no_rates: '尚未配置汇率。',
      validity_type: '默认有效期', reentry: '允许二次入场', max_entries: '最大入场次数',
      expires_at: '到期时间', yes: '是', no: '否',
      pages_word: '页面', actions_word: '操作',
      read_only_banner: '您可以查看此页面，但无法修改。',
      add_only_banner: '您可以在此新增记录，但无法修改已有记录。',
      role_saving_disabled: '此界面暂未开放保存角色权限。下方矩阵为真实权限registry，显示该角色当前持有的权限。',
      inherited_note: '该值继承自更高层级。保存后将为本场馆创建独立设置。',
      weekday: '星期', closed: '休息', open: '开门', close: '关门', last_admission: '最后入场',
      day_mon: '星期一', day_tue: '星期二', day_wed: '星期三', day_thu: '星期四',
      day_fri: '星期五', day_sat: '星期六', day_sun: '星期日',
      enabled_languages: '启用的语言', default_language: '默认语言',
      prefix: '前缀', padding: '编号位数',
      secret: '密钥', secret_on_file: '已保存密钥，尾号', secret_none: '尚未设置密钥。',
      secret_leave_blank: '留空则保留当前密钥',
      endpoint: '接口地址', events: '事件', name: '名称', code: '代码',
      scope_write: '允许写入', active: '启用', add: '新增', none_yet: '尚未配置。',
      discount_pct: '折扣 %', commission_pct: '佣金 %',
      no_records: '此页尚未配置任何内容。', records_word: '条记录',
      records_managed_elsewhere: '记录在各自的模块中创建和编辑。',
      edit: '编辑', delete: '删除', save: '保存',
      status_word: '状态', status_active: '启用', status_inactive: '停用',
      reason_word: '原因',
      display_name_word: '显示名称', code_word: '代码', provider_word: '服务商', channels_word: '渠道',
      help_display_name: '支付时向客户显示。',
      help_code: '系统和集成使用的内部标识符。',
      access_word: '访问权限', assign_role: '分配角色', scope_word: '范围', all_venues: '所有场馆',
      effective_permissions: '有效权限', no_access_anywhere: '该员工目前在任何场馆都没有访问权限。',
      rounding_method: '取整方式', rounding_increment: '取整单位',
      round_up_label: '始终向上取整', round_up_help: '金额始终向上取整。',
      round_down_label: '始终向下取整', round_down_help: '金额始终向下取整。',
      round_half_label: '标准四舍五入', round_half_help: '不足一半向下，达到或超过一半向上。',
      round_none_label: '不取整', round_none_help: '按实际计算金额收取。',
      round_original: '原始金额', round_rounded: '取整后金额',
    },
    ja: {
      login_title: 'おかえりなさい',
      login_lead: 'スタッフ アカウントでサインインしてバックオフィスへ進みます。',
      login_email: 'メールアドレス', login_password: 'パスワード',
      login_show: 'パスワードを表示', login_hide: 'パスワードを隠す',
      login_mfa: '認証コード',
      login_mfa_hint: 'このアカウントは多要素認証が必須です。',
      login_remember: 'この端末でサインインを保持する',
      login_forgot: 'パスワードをお忘れですか？', login_submit: 'サインイン',
      login_help: 'サインインできない場合は管理者にご連絡ください。',
      login_forgot_msg: '管理者が新しい招待リンクを送信できます。この施設ではメールによるパスワード再設定は未対応です。',
      reset_title: 'パスワードを再設定', reset_lead: 'メールアドレスを入力すると、ワンタイムの再設定コードを送信します。',
      reset_code: '再設定コード', reset_new_password: '新しいパスワード',
      reset_password_hint: '12文字以上、大文字・小文字・数字を含めてください。',
      reset_request: '再設定コードを送信', reset_submit: '新しいパスワードを設定', reset_back: 'サインインに戻る',
      reset_code_sent: 'そのメールがスタッフアカウントの場合、再設定コードを送信しました。',
      reset_demo_prefix: 'デモコード：', reset_need_all: 'コードと新しいパスワードを入力してください。',
      reset_done: 'パスワードを更新しました。新しいパスワードでサインインしてください。',
      err_email: 'メールアドレスを入力してください。',
      err_password: 'パスワードを入力してください。',
      session_expired: 'セッションの有効期限が切れました。もう一度サインインしてください。',
      signed_out: 'サインアウトしました。',
      need_signin: '続けるにはサインインしてください。',
      signed_in_as: '{name} としてサインイン中',
      bo_menu: 'メニュー', bo_logout: 'サインアウト', bo_my_access: '自分の権限',
      scope_org: '組織', scope_venue: '施設', scope_setting: '設定の適用範囲',
      scope_level_venue: '施設単位',
      settings_title: '設定',
      settings_lead: 'この施設の販売、料金、入場、レポートの動作を設定します。表示されるのはロールが許可した範囲のみです。',
      search_ph: '設定を検索…',
      no_results: '一致する設定がない、またはアクセス権がありません。',
      recent_used: '最近使用した設定',
      back_to_settings: 'すべての設定', back_to_dashboard: '設定に戻る',
      denied_title: 'このページへのアクセス権限がありません。',
      denied_lead: 'アクセスが必要と思われる場合は管理者にご連絡ください。',
      view_only: '表示のみ', read_only: '読み取り専用', no_access: 'アクセス不可',
      not_applicable: '対象外',
      of_pages: '利用可能 {shown} / {total} 件',
      pages_available: '利用可能 {n} 件',
      protected_note: 'このページのレコードは削除されません。削除は {semantics} を意味します。',
      loading: '読み込み中…',
      perms_changed: '管理者によって権限が更新されました。',
      refresh: '今すぐ再読み込み',
      cancel: 'キャンセル', save_changes: '変更を保存', saved: '保存しました。',
      unsaved: '保存されていない変更があります。',
      discard: '変更を破棄', keep_editing: '編集を続ける',
      confirm_title: '変更の確認', confirm_from: '変更前', confirm_to: '変更後',
      confirm_ok: '変更を確定',
      effective_from: '適用開始日', reason: '理由',
      reason_hint: 'あなたの名前とともに監査ログに記録されます。',
      preview: '計算プレビュー', advanced: '詳細設定',
      enable: '有効にする', rate: '税率',
      mode_included: '内税', mode_excluded: '外税',
      mode_included_help: '表示価格にこの料金が含まれています。',
      mode_excluded_help: '決済時に表示価格へ加算されます。',
      price_shown: '商品価格', before_charge: '税抜金額', charge_amount: '付加価値税',
      sc_amount: 'サービス料', grand_total: '合計',
      history: '変更履歴',
      roles_title: 'ロール', roles_lead: 'ロールを選択して、到達できる範囲を確認または変更します。',
      choose_role: 'ロールを選択', expand_all: 'すべて展開', collapse_all: 'すべて折りたたむ',
      all_view: 'すべて表示', all_add: 'すべて追加', all_edit: 'すべて編集', all_delete: 'すべて削除',
      clear_cat: 'クリア', copy_from: '他のロールから複製…',
      summary_title: '保存前の確認',
      can_view: '表示可', can_add: '追加可', can_edit: '編集可', can_delete: '削除可',
      sensitive: '重要な権限', warnings: '確認事項',
      save_role: 'ロールを保存',
      eff_title: '実効権限',
      eff_lead: 'ロール、スコープ、施設を組み合わせた結果です。すべてのリクエストを検証するコードと同一の処理で解決しています。',
      timezone: 'タイムゾーン', currency: '基準通貨',
      add_rate: '為替レートを追加', end_rate: '終了',
      rate_direction: '1 {from} = {rate} {to}',
      no_rates: '為替レートは未設定です。',
      validity_type: '既定の有効期限', reentry: '再入場を許可', max_entries: '最大入場回数',
      expires_at: '有効期限時刻', yes: 'はい', no: 'いいえ',
      pages_word: 'ページ', actions_word: '操作',
      read_only_banner: 'このページは閲覧できますが、変更はできません。',
      add_only_banner: '新規レコードは追加できますが、既存レコードは変更できません。',
      role_saving_disabled: 'この画面からのロール権限の保存はまだ有効ではありません。下の表は実際のレジストリで、このロールが現在保持している権限を表示しています。',
      inherited_note: 'この値は上位スコープから継承されています。保存するとこの施設用の上書きが作成されます。',
      weekday: '曜日', closed: '休業', open: '開館', close: '閉館', last_admission: '最終入場',
      day_mon: '月曜', day_tue: '火曜', day_wed: '水曜', day_thu: '木曜',
      day_fri: '金曜', day_sat: '土曜', day_sun: '日曜',
      enabled_languages: '有効な言語', default_language: '既定の言語',
      prefix: '接頭辞', padding: '桁数',
      secret: 'シークレット', secret_on_file: 'シークレット登録済み、末尾', secret_none: 'シークレット未設定。',
      secret_leave_blank: '空欄のままにすると現在のシークレットを保持します',
      endpoint: 'エンドポイント URL', events: 'イベント', name: '名前', code: 'コード',
      scope_write: '書き込みを許可', active: '有効', add: '追加', none_yet: 'まだ設定されていません。',
      discount_pct: '割引 %', commission_pct: '手数料 %',
      no_records: 'このページにはまだ何も設定されていません。', records_word: '件',
      records_managed_elsewhere: 'レコードは各モジュールで作成・編集します。',
      edit: '編集', delete: '削除', save: '保存',
      status_word: 'ステータス', status_active: '有効', status_inactive: '無効',
      reason_word: '理由',
      display_name_word: '表示名', code_word: 'コード', provider_word: 'プロバイダー', channels_word: 'チャネル',
      help_display_name: '支払い時に顧客に表示されます。',
      help_code: 'システムと連携で使用する内部識別子。',
      access_word: 'アクセス', assign_role: 'ロールを割り当て', scope_word: '範囲', all_venues: 'すべての会場',
      effective_permissions: '有効な権限', no_access_anywhere: 'このスタッフはまだどの会場にもアクセス権がありません。',
      rounding_method: '端数処理の方法', rounding_increment: '端数処理の単位',
      round_up_label: '常に切り上げ', round_up_help: '金額を常に切り上げます。',
      round_down_label: '常に切り捨て', round_down_help: '金額を常に切り捨てます。',
      round_half_label: '標準的な四捨五入', round_half_help: '半分未満は切り捨て、半分以上は切り上げ。',
      round_none_label: '端数処理なし', round_none_help: '計算された正確な金額を請求します。',
      round_original: '元の金額', round_rounded: '処理後の金額',
    },
    ru: {
      login_title: 'С возвращением',
      login_lead: 'Войдите под учётной записью сотрудника, чтобы открыть бэк-офис.',
      login_email: 'Электронная почта', login_password: 'Пароль',
      login_show: 'Показать пароль', login_hide: 'Скрыть пароль',
      login_mfa: 'Код аутентификатора',
      login_mfa_hint: 'Для этой учётной записи обязательна многофакторная аутентификация.',
      login_remember: 'Оставаться в системе на этом устройстве',
      login_forgot: 'Забыли пароль?', login_submit: 'Войти',
      login_help: 'Не удаётся войти? Обратитесь к администратору.',
      login_forgot_msg: 'Администратор может отправить вам новую ссылку-приглашение. Сброс пароля по электронной почте для этого объекта пока не включён.',
      reset_title: 'Сброс пароля', reset_lead: 'Введите свой адрес электронной почты — мы отправим одноразовый код сброса.',
      reset_code: 'Код сброса', reset_new_password: 'Новый пароль',
      reset_password_hint: 'Не менее 12 символов, с заглавными и строчными буквами и цифрой.',
      reset_request: 'Отправить код', reset_submit: 'Задать новый пароль', reset_back: 'Назад ко входу',
      reset_code_sent: 'Если этот адрес принадлежит учётной записи сотрудника, код сброса отправлен.',
      reset_demo_prefix: 'Демо-код:', reset_need_all: 'Введите код и новый пароль.',
      reset_done: 'Пароль обновлён. Войдите с новым паролем.',
      err_email: 'Введите адрес электронной почты.',
      err_password: 'Введите пароль.',
      session_expired: 'Сеанс истёк. Пожалуйста, войдите снова.',
      signed_out: 'Вы вышли из системы.',
      need_signin: 'Войдите, чтобы продолжить.',
      signed_in_as: 'Вы вошли как {name}',
      bo_menu: 'Меню', bo_logout: 'Выйти', bo_my_access: 'Мои права',
      scope_org: 'Организация', scope_venue: 'Объект', scope_setting: 'Область настройки',
      scope_level_venue: 'Уровень объекта',
      settings_title: 'Настройки',
      settings_lead: 'Настройте продажи, цены, вход и отчётность этого объекта. Вы видите только то, что разрешает ваша роль.',
      search_ph: 'Поиск настроек…',
      no_results: 'Ничего не найдено, либо у вас нет доступа к этим настройкам.',
      recent_used: 'Недавно использованные',
      back_to_settings: 'Все настройки', back_to_dashboard: 'Назад к настройкам',
      denied_title: 'У вас нет прав для доступа к этой странице.',
      denied_lead: 'Если вам нужен доступ, обратитесь к администратору.',
      view_only: 'Только просмотр', read_only: 'Только чтение', no_access: 'Нет доступа',
      not_applicable: 'Не применимо',
      of_pages: 'Доступно {shown} из {total}',
      pages_available: 'Доступно настроек: {n}',
      protected_note: 'Записи на этой странице не удаляются. Удаление означает: {semantics}.',
      loading: 'Загрузка…',
      perms_changed: 'Администратор изменил ваши права доступа.',
      refresh: 'Перезагрузить сейчас',
      cancel: 'Отмена', save_changes: 'Сохранить изменения', saved: 'Сохранено.',
      unsaved: 'Есть несохранённые изменения.',
      discard: 'Отменить изменения', keep_editing: 'Продолжить редактирование',
      confirm_title: 'Подтвердите изменение', confirm_from: 'Было', confirm_to: 'Станет',
      confirm_ok: 'Подтвердить изменение',
      effective_from: 'Действует с', reason: 'Причина',
      reason_hint: 'Будет записано в журнал аудита вместе с вашим именем.',
      preview: 'Предпросмотр расчёта', advanced: 'Расширенные настройки',
      enable: 'Включить', rate: 'Ставка',
      mode_included: 'Включено в цену', mode_excluded: 'Сверх цены',
      mode_included_help: 'Указанная цена продажи уже содержит этот сбор.',
      mode_excluded_help: 'Сбор добавляется к цене продажи при оплате.',
      price_shown: 'Цена товара', before_charge: 'Сумма без НДС', charge_amount: 'НДС',
      sc_amount: 'Сервисный сбор', grand_total: 'Итого',
      history: 'История изменений',
      roles_title: 'Роли', roles_lead: 'Выберите роль, чтобы проверить или изменить её доступ.',
      choose_role: 'Выберите роль', expand_all: 'Развернуть всё', collapse_all: 'Свернуть всё',
      all_view: 'Всё просмотр', all_add: 'Всё добавление', all_edit: 'Всё изменение', all_delete: 'Всё удаление',
      clear_cat: 'Очистить', copy_from: 'Копировать из роли…',
      summary_title: 'Перед сохранением',
      can_view: 'Просмотр', can_add: 'Добавление', can_edit: 'Изменение', can_delete: 'Удаление',
      sensitive: 'Особо важный доступ', warnings: 'Стоит проверить',
      save_role: 'Сохранить роль',
      eff_title: 'Действующие права',
      eff_lead: 'Результат объединения роли, области и объекта — вычислен тем же кодом, который проверяет каждый запрос.',
      timezone: 'Часовой пояс', currency: 'Базовая валюта',
      add_rate: 'Добавить курс', end_rate: 'Завершить',
      rate_direction: '1 {from} = {rate} {to}',
      no_rates: 'Курсы обмена не настроены.',
      validity_type: 'Срок действия по умолчанию', reentry: 'Повторный вход разрешён', max_entries: 'Максимум входов',
      expires_at: 'Истекает в', yes: 'Да', no: 'Нет',
      pages_word: 'Страницы', actions_word: 'Действия',
      read_only_banner: 'Вы можете просматривать эту страницу, но не изменять её.',
      add_only_banner: 'Вы можете добавлять новые записи, но не изменять существующие.',
      role_saving_disabled: 'Сохранение прав роли с этого экрана пока не включено. Таблица ниже — действующий реестр и показывает, что роль имеет сейчас.',
      inherited_note: 'Значение унаследовано от более высокого уровня. Сохранение создаст переопределение для этого объекта.',
      weekday: 'День', closed: 'Закрыто', open: 'Открытие', close: 'Закрытие', last_admission: 'Последний вход',
      day_mon: 'Понедельник', day_tue: 'Вторник', day_wed: 'Среда', day_thu: 'Четверг',
      day_fri: 'Пятница', day_sat: 'Суббота', day_sun: 'Воскресенье',
      enabled_languages: 'Доступные языки', default_language: 'Язык по умолчанию',
      prefix: 'Префикс', padding: 'Разрядность номера',
      secret: 'Секрет', secret_on_file: 'Секрет сохранён, оканчивается на', secret_none: 'Секрет ещё не задан.',
      secret_leave_blank: 'Оставьте пустым, чтобы сохранить текущий секрет',
      endpoint: 'URL эндпоинта', events: 'События', name: 'Название', code: 'Код',
      scope_write: 'Разрешить запись', active: 'Активно', add: 'Добавить', none_yet: 'Пока ничего не настроено.',
      discount_pct: 'Скидка %', commission_pct: 'Комиссия %',
      no_records: 'На этой странице пока ничего не настроено.', records_word: 'записей',
      records_managed_elsewhere: 'Записи создаются и редактируются в своих модулях.',
      edit: 'Изменить', delete: 'Удалить', save: 'Сохранить',
      status_word: 'Статус', status_active: 'Активно', status_inactive: 'Неактивно',
      reason_word: 'Причина',
      display_name_word: 'Отображаемое имя', code_word: 'Код', provider_word: 'Провайдер', channels_word: 'Каналы',
      help_display_name: 'Показывается клиентам при оплате.',
      help_code: 'Внутренний идентификатор для системы и интеграций.',
      access_word: 'Доступ', assign_role: 'Назначить роль', scope_word: 'Область', all_venues: 'Все площадки',
      effective_permissions: 'Действующие права', no_access_anywhere: 'У этого сотрудника пока нет доступа ни к одной площадке.',
      rounding_method: 'Метод округления', rounding_increment: 'Шаг округления',
      round_up_label: 'Всегда вверх', round_up_help: 'Всегда округлять сумму вверх.',
      round_down_label: 'Всегда вниз', round_down_help: 'Всегда округлять сумму вниз.',
      round_half_label: 'Обычное округление', round_half_help: 'Менее половины — вниз, половина и более — вверх.',
      round_none_label: 'Без округления', round_none_help: 'Взимать точную рассчитанную сумму.',
      round_original: 'Исходная сумма', round_rounded: 'После округления',
    },
  };

  function t(key, vars) {
    const table = T[lang()] || T.en;
    let out = table[key] || T.en[key] || key;
    if (vars) Object.keys(vars).forEach((k) => { out = out.split(`{${k}}`).join(String(vars[k])); });
    return out;
  }

  /* ------------------------------------------------------------- utilities */

  const esc = (s) => String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  // Page keys contain spaces and ampersands ("Terms & Conditions"), so a URL needs a
  // slug. The mapping is one-way in code and reversed through a lookup built from the
  // server's own page list, never by un-slugifying — that would guess at a key the
  // server is authoritative for.
  const slug = (key) => String(key).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');

  const money = (minor, currency, decimals) => {
    const d = typeof decimals === 'number' ? decimals : 2;
    const value = minor / Math.pow(10, d);
    try {
      return new Intl.NumberFormat(window.utpLocale ? window.utpLocale() : 'en',
        { style: 'currency', currency: currency || 'THB', minimumFractionDigits: d, maximumFractionDigits: d })
        .format(value);
    } catch (_) { return `${currency || ''} ${value.toFixed(d)}`; }
  };

  const pct = (bp) => `${(bp / 100).toFixed(2)}%`;

  /* ------------------------------------------------------------------ state */

  const S = {
    profile: null,        // last /api/staff/me response
    profileAt: 0,         // when it was fetched (ms)
    pageIndex: {},        // slug -> page key, built from the profile's settings tree
    categoryOf: {},       // page key -> category key
    intended: null,       // route to return to after signing in (§5)
    settings: null,       // /api/staff/settings blocks
    matrix: null,         // /api/staff/permissions/matrix
    dirty: null,          // { describe() } when a form has unsaved edits (§39)
    notice: null,         // message to show on the login page
    recent: [],           // recently used settings pages (§33)
  };

  const PROFILE_TTL_MS = 15000;
  const RECENT_KEY = 'utp_bo_recent';

  try { S.recent = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]') || []; } catch (_) { S.recent = []; }

  function rememberPage(pageKey) {
    S.recent = [pageKey].concat(S.recent.filter((p) => p !== pageKey)).slice(0, 5);
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(S.recent)); } catch (_) {}
  }

  /* --------------------------------------------------------- permissions
   *
   * The one helper every screen uses. It reads the permission set the server
   * resolved for this principal at this venue — not a role name, not a flag the
   * client set for itself (§48).
   */
  function can(page, verb) {
    if (!S.profile) return false;
    return S.profile.permissions.indexOf(`${page}.${verb || 'VIEW'}`) !== -1;
  }

  function canAction(action) {
    if (!S.profile) return false;
    return S.profile.permissions.indexOf(`ACTION:${action}`) !== -1;
  }

  function categories() { return (S.profile && S.profile.settings) || []; }

  function findPage(pageKey) {
    let found = null;
    categories().forEach((c) => c.pages.forEach((p) => { if (p.page === pageKey) found = { page: p, category: c }; }));
    return found;
  }

  /* ------------------------------------------------------------------ router
   *
   * Route table. Each protected entry names the permission the *server* will
   * demand, so the guard and the endpoint cannot drift apart. Settings page routes
   * resolve their permission from the page key in the URL.
   */
  const PUBLIC_VIEWS = { '/book': 'book', '/shows': 'shows', '/manage': 'manage' };

  function parseRoute(hash) {
    const path = (hash || '').replace(/^#/, '') || '/book';
    const parts = path.split('/').filter(Boolean);
    return { path: '/' + parts.join('/'), parts: parts };
  }

  function go(path, options) {
    const target = '#' + (path.charAt(0) === '/' ? path : '/' + path);
    if (options && options.replace && location.replace) {
      location.replace(location.pathname + location.search + target);
    } else if (location.hash === target) {
      handleRoute();
    } else {
      location.hash = target;
    }
  }

  // Where a signed-in user lands when they did not ask for anything specific (§62).
  // Chosen from what they can actually reach, so a report-only account is not sent
  // to a Settings page it will be refused.
  function landingRoute() {
    if (can('Dashboard')) return '/dashboard';
    if (categories().length) return '/settings';
    if (can('Reports')) return '/reports';
    if (can('Operations Dashboard')) return '/operations';
    return '/denied';
  }

  async function loadProfile(force) {
    if (!auth.token) { S.profile = null; return null; }
    const fresh = S.profile && !force && (Date.now() - S.profileAt) < PROFILE_TTL_MS;
    if (fresh) return S.profile;
    const data = await api('/api/staff/me');
    S.profile = data;
    S.profileAt = Date.now();
    S.pageIndex = {};
    S.categoryOf = {};
    (data.settings || []).forEach((c) => c.pages.forEach((p) => {
      S.pageIndex[slug(p.page)] = p.page;
      S.categoryOf[p.page] = c.category;
    }));
    // §53, §74: the server compares the session's permission epoch with the staff
    // row's. When they differ an administrator changed this account's access while
    // it was signed in, so we say so instead of letting the difference surface as a
    // random refusal later.
    if (data.permissions_changed) {
      toast(t('perms_changed'), 'info');
      S.profileAt = 0;
    }
    return data;
  }

  async function handleRoute() {
    const route = parseRoute(location.hash);
    const path = route.path;

    if (PUBLIC_VIEWS[path]) { showView(PUBLIC_VIEWS[path]); return; }
    if (path === '/login') { renderLogin(); return; }

    // §5: an unauthenticated request for a protected route goes to Login, and the
    // destination is remembered so the user is returned there afterwards.
    if (!auth.token) {
      S.intended = path;
      S.notice = S.notice || t('need_signin');
      renderLogin();
      return;
    }

    let profile;
    try {
      profile = await loadProfile();
    } catch (e) {
      if (e.status === 401) { S.notice = t('session_expired'); renderLogin(); return; }
      showView('backoffice');
      $('boBody').innerHTML = `<div class="bo-empty"><h2>${esc(e.message)}</h2></div>`;
      return;
    }
    if (!profile) { renderLogin(); return; }

    renderShell(path);

    if (path === '/denied') { renderDenied(); return; }
    if (path === '/reports') {
      // Reports live in their own module but are protected the same way (§64, §72).
      if (!can('Reports') && !can('Dashboard') && !can('Operations Dashboard')) { renderDenied('Reports'); return; }
      showView('reports');
      if (window.utpReports) window.utpReports.open();
      return;
    }
    if (path === '/operations') { showView('ops'); return; }
    if (path === '/dashboard') {
      if (!can('Dashboard')) { renderDenied('Dashboard'); return; }
      showView('reports');
      if (window.utpReports) window.utpReports.open();
      return;
    }
    if (path === '/access') { await renderMyAccess(); return; }
    if (path === '/roles') {
      if (!can('Roles') && !can('Permissions')) { renderDenied('Roles'); return; }
      await renderRoles();
      return;
    }
    if (route.parts[0] === 'settings') {
      if (route.parts.length === 1) {
        if (!categories().length) { renderDenied('Settings'); return; }
        renderSettingsHome();
        return;
      }
      const categoryKey = route.parts[1];
      const category = categories().filter((c) => c.category === categoryKey)[0];
      // §71: a category with nothing viewable inside it is not a page this user has.
      if (!category) { renderDenied(); return; }
      if (route.parts.length === 2) { renderCategory(category); return; }
      const pageKey = S.pageIndex[route.parts[2]];
      // §14, §66: no VIEW means the direct route is refused, not just hidden.
      if (!pageKey || !can(pageKey)) { renderDenied(); return; }
      await renderSettingsPage(pageKey, category);
      return;
    }
    // Anything unrecognised behind the guard: send the user somewhere they can be.
    go(landingRoute(), { replace: true });
  }

  /* ------------------------------------------------------------------- login */

  function loginField(id, message) {
    const node = $(id);
    if (!node) return;
    node.textContent = message || '';
    node.hidden = !message;
  }

  function renderLogin() {
    showView('login');
    const notice = $('loginNotice');
    notice.textContent = S.notice || '';
    notice.hidden = !S.notice;
    S.notice = null;
    applyLoginText();
    const email = $('loginEmail');
    if (email && !email.value) email.focus();
  }

  // The login page is static markup, so its labels are translated here on each show
  // rather than re-rendered.
  function applyLoginText() {
    const map = {
      loginNotice: null,
      loginLead: 'login_lead',
      loginHelp: 'login_help',
      loginRevealText: 'login_show',
    };
    Object.keys(map).forEach((id) => { if (map[id] && $(id)) $(id).textContent = t(map[id]); });
    const setText = (sel, key) => {
      const node = document.querySelector(sel);
      if (node) node.textContent = t(key);
    };
    setText('.lg-title', 'login_title');
    setText('label[for="loginEmail"]', 'login_email');
    setText('label[for="loginPass"]', 'login_password');
    setText('label[for="loginMfa"]', 'login_mfa');
    setText('#loginMfaField .hint', 'login_mfa_hint');
    setText('.lg-check span', 'login_remember');
    setText('#loginForgot', 'login_forgot');
    setText('#loginSubmit', 'login_submit');
  }

  async function submitLogin(event) {
    if (event) event.preventDefault();
    const email = $('loginEmail').value.trim();
    const credential = $('loginPass').value;
    const mfa = $('loginMfa').value.trim();
    loginField('loginEmailErr', '');
    loginField('loginPassErr', '');
    $('loginNotice').hidden = true;

    // Client-side checks are courtesy, in business language, and stop before the
    // network so an empty field is not reported as an authentication failure (§2).
    if (!email) { loginField('loginEmailErr', t('err_email')); $('loginEmail').focus(); return; }
    if (!credential) { loginField('loginPassErr', t('err_password')); $('loginPass').focus(); return; }

    const button = $('loginSubmit');
    button.disabled = true;
    try {
      const body = { email: email, credential: credential };
      if (mfa) body.mfa_code = mfa;
      const result = await api('/api/staff/login', { method: 'POST', body: JSON.stringify(body) });
      auth.set(result.token);
      S.profile = null;
      S.profileAt = 0;
      $('loginPass').value = '';
      $('loginMfa').value = '';
      $('loginMfaField').hidden = true;
      await loadProfile(true);
      toast(t('signed_in_as', { name: result.display_name }), 'success');
      const target = S.intended;
      S.intended = null;
      // Return the user where they were headed, but only if they may go there (§5).
      go(target && target !== '/login' ? target : landingRoute(), { replace: true });
    } catch (e) {
      // §2: whatever the server said is what the operator sees. It is already
      // business language — "Email or password is incorrect", "Your account is
      // inactive" — and it never carries a stack trace or a provider code.
      if (e.code === 'mfa_required') {
        $('loginMfaField').hidden = false;
        $('loginMfa').focus();
      }
      const perField = e.details && e.details.fields;
      if (perField && perField.mfa_code) {
        loginField('loginPassErr', perField.mfa_code);
      } else {
        loginField('loginPassErr', e.message);
      }
      const notice = $('loginNotice');
      notice.textContent = e.message;
      notice.hidden = false;
    } finally {
      button.disabled = false;
    }
  }

  async function signOut() {
    try { await api('/api/staff/logout', { method: 'POST' }); } catch (_) { /* already gone */ }
    auth.clear('signed-out');
    S.profile = null;
    S.settings = null;
    S.matrix = null;
    S.intended = null;
    S.notice = t('signed_out');
    // §58: every protected route is now unreachable, which the guard enforces on the
    // very next navigation because the token is gone.
    go('/login', { replace: true });
  }

  /* ------------------------------------------------------------------- shell */

  function renderShell(activePath) {
    showView('backoffice');
    const profile = S.profile;
    if (!profile) return;

    // --- sidebar, built from effective VIEW permissions (§45, §70) --- //
    const groups = [];
    if (can('Dashboard')) groups.push({ route: '/dashboard', label: pageLabel('Dashboard') });
    if (can('Reports')) groups.push({ route: '/reports', label: pageLabel('Reports') });
    const cats = categories();
    const side = [];
    side.push(`<div class="bo-side-head"><span class="bo-logo" aria-hidden="true"></span>
      <span class="bo-side-title">${esc(t('settings_title'))}</span></div>`);
    if (groups.length) {
      side.push(`<nav class="bo-nav" aria-label="${esc(t('pages_word'))}"><ul>${groups.map((g) =>
        `<li><button type="button" class="bo-link${activePath === g.route ? ' is-active' : ''}" data-route="${esc(g.route)}">${esc(g.label)}</button></li>`
      ).join('')}</ul></nav>`);
    }
    if (cats.length) {
      side.push(`<nav class="bo-nav bo-nav-settings" aria-label="${esc(t('settings_title'))}">
        <button type="button" class="bo-link bo-link-all${activePath === '/settings' ? ' is-active' : ''}"
                data-route="/settings">${esc(t('back_to_settings'))}</button>
        ${cats.map((c) => {
          const open = activePath.indexOf(`/settings/${c.category}`) === 0;
          const catIco = window.utpIcons ? window.utpIcons.categoryIcon(c.category, { size: 18 }) : '';
          return `<details class="bo-group"${open ? ' open' : ''}>
            <summary><span class="bo-ico" aria-hidden="true">${catIco}</span>
              <span>${esc(c.label)}</span><span class="bo-count">${c.page_count}</span></summary>
            <ul>${c.pages.map((p) => `<li><button type="button"
                class="bo-link${activePath === `/settings/${c.category}/${slug(p.page)}` ? ' is-active' : ''}"
                data-route="/settings/${esc(c.category)}/${esc(slug(p.page))}"><span class="bo-link-ico" aria-hidden="true">${icon(window.utpIcons ? window.utpIcons.slugForSettings(p.page) : '', 16)}</span><span class="bo-link-label">${esc(p.label)}</span>${
                  p.can_edit ? '' : `<span class="bo-ro" title="${esc(t('read_only'))}">${esc(t('view_only'))}</span>`
                }</button></li>`).join('')}</ul>
          </details>`;
        }).join('')}
      </nav>`);
    }
    if (!groups.length && !cats.length) {
      side.push(`<p class="bo-side-empty">${esc(t('denied_lead'))}</p>`);
    }
    $('boSide').innerHTML = side.join('');
    $('boSide').querySelectorAll('[data-route]').forEach((b) =>
      b.addEventListener('click', () => navigate(b.dataset.route)));

    // --- scope banner (§34) --- //
    const venue = (profile.venues || []).filter((v) => v.id === profile.scope.current_venue_id)[0]
      || (profile.venues || [])[0];
    const venueName = venue ? (venue.name && (venue.name[lang()] || venue.name.en) || venue.code) : '—';
    $('boScope').innerHTML = `
      <span class="bo-scope-item"><small>${esc(t('scope_org'))}</small><strong>${
        esc(profile.organization ? profile.organization.name : (profile.tenant ? profile.tenant.name : '—'))}</strong></span>
      <span class="bo-scope-item"><small>${esc(t('scope_venue'))}</small><strong>${esc(venueName)}</strong></span>
      <span class="bo-scope-item"><small>${esc(t('scope_setting'))}</small><strong>${esc(t('scope_level_venue'))}</strong></span>`;

    // --- profile menu with sign out (§58) --- //
    const roles = (profile.roles || []).map((r) => r.name).join(', ');
    $('boProfile').innerHTML = `
      <details class="bo-user">
        <summary>
          <span class="bo-avatar" aria-hidden="true">${esc((profile.staff.display_name || '?').slice(0, 1))}</span>
          <span class="bo-user-text"><strong>${esc(profile.staff.display_name)}</strong><small>${esc(roles)}</small></span>
        </summary>
        <div class="bo-user-menu">
          <p class="bo-user-meta">${esc(profile.staff.email)}</p>
          <button type="button" class="bo-user-item" data-route="/access">${esc(t('bo_my_access'))}</button>
          <button type="button" class="bo-user-item is-danger" id="boSignOut">${esc(t('bo_logout'))}</button>
        </div>
      </details>`;
    $('boProfile').querySelectorAll('[data-route]').forEach((b) =>
      b.addEventListener('click', () => navigate(b.dataset.route)));
    $('boSignOut').addEventListener('click', signOut);
  }

  function pageLabel(pageKey) {
    const nav = ((S.profile && S.profile.navigation) || []).filter((n) => n.page === pageKey)[0];
    return nav ? nav.label : pageKey;
  }

  // §39: leaving a form with unsaved edits asks first rather than silently losing them.
  function navigate(path) {
    if (S.dirty) {
      const keep = !window.confirm(`${t('unsaved')}\n\n${t('discard')}?`);
      if (keep) return;
      S.dirty = null;
    }
    go(path);
  }

  function crumb(items) {
    const node = $('boCrumb');
    if (!items || !items.length) { node.hidden = true; node.innerHTML = ''; return; }
    node.hidden = false;
    node.innerHTML = items.map((item, i) => {
      const last = i === items.length - 1;
      return last
        ? `<span aria-current="page">${esc(item.label)}</span>`
        : `<button type="button" class="link-btn" data-route="${esc(item.route)}">${esc(item.label)}</button><span class="bo-crumb-sep" aria-hidden="true">/</span>`;
    }).join('');
    node.querySelectorAll('[data-route]').forEach((b) =>
      b.addEventListener('click', () => navigate(b.dataset.route)));
  }

  /* ------------------------------------------------------- access denied (§6) */

  function renderDenied(what) {
    showView('backoffice');
    crumb(null);
    // Deliberately not a redirect to Login: the user *is* signed in, and bouncing
    // them to a login form they will pass again produces the redirect loop §6 calls
    // out. It is also not told which permission is missing (R42.3).
    $('boBody').innerHTML = `
      <div class="bo-denied">
        <span class="bo-denied-ico" aria-hidden="true"></span>
        <h2>${esc(t('denied_title'))}</h2>
        <p>${esc(t('denied_lead'))}</p>
        ${what ? `<p class="bo-denied-what">${esc(what)}</p>` : ''}
        <button type="button" class="primary" id="boDeniedBack">${esc(t('back_to_dashboard'))}</button>
      </div>`;
    $('boDeniedBack').addEventListener('click', () => go(landingRoute()));
  }

  /* --------------------------------------------------- settings home (§11, §59) */

  function icon(slug, size) {
    return window.utpIcons ? window.utpIcons.markup(slug, { size: size || 20 }) : '';
  }

  function categoryCard(category) {
    const status = categoryStatus(category);
    const ico = window.utpIcons ? window.utpIcons.categoryIcon(category.category, { size: 24 }) : '';
    return `<button type="button" class="bo-card" data-route="/settings/${esc(category.category)}">
      <span class="bo-card-ico uic-tile" aria-hidden="true">${ico}</span>
      <span class="bo-card-text">
        <strong>${esc(category.label)}</strong>
        <small>${esc(category.description)}</small>
        ${status ? `<em class="bo-card-status">${esc(status)}</em>` : ''}
      </span>
      <span class="bo-card-meta">${esc(category.page_count === category.total_pages
        ? t('pages_available', { n: category.page_count })
        : t('of_pages', { shown: category.page_count, total: category.total_pages }))}</span>
      <span class="bo-card-go" aria-hidden="true"></span>
    </button>`;
  }

  // §59 wants a card to say something concrete — "VAT: 7% Included", "4 payment
  // methods active" — rather than only a page count. Only facts already loaded are
  // shown; the home does not fetch every category's contents to decorate itself.
  function categoryStatus(category) {
    const s = S.settings;
    if (!s) return '';
    if (category.category === 'pricing_tax' && s.vat && s.vat.current) {
      const vat = s.vat.current;
      if (!vat.enabled) return `${pageLabel('VAT Settings')}: ${t('no')}`;
      return `${vat.display_name || 'VAT'} ${pct(vat.rate_bp)} · ${
        vat.mode === 'INCLUSIVE' ? t('mode_included') : t('mode_excluded')}`;
    }
    if (category.category === 'business' && s.timezone) return `${t('timezone')}: ${s.timezone.timezone}`;
    if (category.category === 'payment' && typeof s.payment_type_count === 'number') {
      return `${s.payment_type_count} × ${pageLabel('Payment Type')}`;
    }
    return '';
  }

  async function renderSettingsHome() {
    crumb([{ label: t('settings_title') }]);
    const cats = categories();
    $('boBody').innerHTML = `
      <header class="bo-head">
        <h2>${esc(t('settings_title'))}</h2>
        <p class="bo-lead">${esc(t('settings_lead'))}</p>
      </header>
      <div class="bo-search">
        <label class="sr-only" for="boSearch">${esc(t('search_ph'))}</label>
        <input id="boSearch" type="search" placeholder="${esc(t('search_ph'))}" autocomplete="off" spellcheck="false">
        <div id="boSearchOut" class="bo-search-out" hidden></div>
      </div>
      <div id="boRecent"></div>
      <div class="bo-cards">${cats.map(categoryCard).join('')}</div>`;
    bindRoutes($('boBody'));
    bindSearch();
    renderRecent();
    // Load the settings blocks once so the cards can carry a real status line. A
    // failure here must not break the page: the map still works without decoration.
    if (!S.settings) {
      try {
        S.settings = await api('/api/staff/settings');
        if (can('Payment Type')) {
          try {
            const pt = await api('/api/staff/payment-types');
            S.settings.payment_type_count = (pt.payment_types || []).filter((p) => p.status === 'ACTIVE').length;
          } catch (_) {}
        }
      } catch (_) { S.settings = null; }
      if (S.settings && !$('boBody').querySelector('.bo-cards')) return;
      if (S.settings) {
        const holder = $('boBody').querySelector('.bo-cards');
        if (holder) { holder.innerHTML = cats.map(categoryCard).join(''); bindRoutes(holder); }
      }
    }
  }

  function renderRecent() {
    const node = $('boRecent');
    if (!node) return;
    const usable = S.recent.filter((p) => can(p) && findPage(p));
    if (!usable.length) { node.innerHTML = ''; return; }
    node.innerHTML = `<div class="bo-recent"><span class="bo-recent-label">${esc(t('recent_used'))}</span>${
      usable.map((p) => {
        const hit = findPage(p);
        return `<button type="button" class="bo-chip" data-route="/settings/${esc(hit.category.category)}/${esc(slug(p))}">${esc(hit.page.label)}</button>`;
      }).join('')}</div>`;
    bindRoutes(node);
  }

  function bindRoutes(root) {
    root.querySelectorAll('[data-route]').forEach((b) => {
      if (b._bound) return;
      b._bound = true;
      b.addEventListener('click', () => navigate(b.dataset.route));
    });
  }

  /* ----------------------------------------------------- settings search (§32) */

  function bindSearch() {
    const input = $('boSearch');
    const out = $('boSearchOut');
    if (!input) return;
    let timer = null;
    input.addEventListener('input', () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (!q) { out.hidden = true; out.innerHTML = ''; return; }
      // Debounced, and answered by the server so the result set is filtered by the
      // same permissions that guard the pages (§27). Searching a client-side copy
      // would reveal pages this user may not open.
      timer = setTimeout(async () => {
        try {
          const data = await api(`/api/staff/settings/search?q=${encodeURIComponent(q)}`);
          const results = data.results || [];
          out.hidden = false;
          out.innerHTML = results.length
            ? results.map((r) => `<button type="button" class="bo-result"
                data-route="/settings/${esc(r.category)}/${esc(slug(r.page))}">
                <span class="bo-result-ico" aria-hidden="true">${icon(window.utpIcons ? window.utpIcons.slugForSettings(r.page) : '', 18)}</span>
                <span class="bo-result-text">
                  <span class="bo-result-cat">${esc(r.category_label)}</span>
                  <span class="bo-result-page">${esc(r.label)}</span>
                </span>
                ${r.can_edit ? '' : `<span class="bo-ro">${esc(t('view_only'))}</span>`}
              </button>`).join('')
            : `<p class="bo-result-empty">${esc(t('no_results'))}</p>`;
          bindRoutes(out);
        } catch (e) { out.hidden = true; }
      }, 180);
    });
  }

  /* -------------------------------------------------------- category listing */

  function renderCategory(category) {
    crumb([{ label: t('settings_title'), route: '/settings' }, { label: category.label }]);
    $('boBody').innerHTML = `
      <header class="bo-head">
        <h2><span class="bo-head-ico uic-tile" aria-hidden="true">${window.utpIcons ? window.utpIcons.categoryIcon(category.category, { size: 24 }) : ''}</span>${esc(category.label)}</h2>
        <p class="bo-lead">${esc(category.description)}</p>
      </header>
      <ul class="bo-pagelist">${category.pages.map((p) => `
        <li><button type="button" class="bo-pagerow" data-route="/settings/${esc(category.category)}/${esc(slug(p.page))}">
          <span class="bo-pagerow-ico uic-tile" aria-hidden="true">${icon(window.utpIcons ? window.utpIcons.slugForSettings(p.page) : '', 20)}</span>
          <span class="bo-pagerow-main">
            <strong>${esc(p.label)}</strong>
            ${p.description ? `<small>${esc(p.description)}</small>` : ''}
          </span>
          <span class="bo-verbs">${verbBadges(p)}</span>
          <span class="bo-card-go" aria-hidden="true"></span>
        </button></li>`).join('')}</ul>`;
    bindRoutes($('boBody'));
  }

  // The four independent verbs, shown as they actually are (§9). A verb the page
  // does not declare renders as "not applicable" rather than as an unchecked box,
  // because granting it would never be honoured (§13's "-" cell).
  function verbBadges(page) {
    const order = ['VIEW', 'ADD', 'EDIT', 'DELETE'];
    const held = { VIEW: page.can_view, ADD: page.can_add, EDIT: page.can_edit, DELETE: page.can_delete };
    return order.map((verb) => {
      if (!page.verbs[verb]) return `<span class="bo-verb is-na" title="${esc(t('not_applicable'))}">–</span>`;
      const on = !!held[verb];
      const label = verbLabel(verb);
      return `<span class="bo-verb${on ? ' is-on' : ''}" title="${esc(label)}">${esc(label.slice(0, 1))}<span class="sr-only">${esc(label)}</span></span>`;
    }).join('');
  }

  function verbLabel(verb) {
    if (S.matrix) {
      const hit = S.matrix.verbs.filter((v) => v.verb === verb)[0];
      if (hit) return hit.label;
    }
    return { VIEW: t('can_view'), ADD: t('can_add'), EDIT: t('can_edit'), DELETE: t('can_delete') }[verb] || verb;
  }

  /* ------------------------------------------------- settings page (§30, §38-42) */

  async function renderSettingsPage(pageKey, category) {
    rememberPage(pageKey);
    const meta = (category.pages || []).filter((p) => p.page === pageKey)[0] || {};
    crumb([
      { label: t('settings_title'), route: '/settings' },
      { label: category.label, route: `/settings/${category.category}` },
      { label: meta.label || pageKey },
    ]);
    const body = $('boBody');
    body.innerHTML = `<p class="bo-loading">${esc(t('loading'))}</p>`;

    // Read-only state is the default, not an afterthought: a page the user may view
    // but not edit renders as values with no controls (§16, §68), and the API would
    // refuse the write anyway.
    const banner = !meta.can_edit
      ? `<p class="bo-banner is-info">${esc(t('read_only_banner'))}</p>`
      : (meta.can_add && !meta.can_edit ? `<p class="bo-banner is-info">${esc(t('add_only_banner'))}</p>` : '');

    let inner = '';
    try {
      inner = await settingsPageBody(pageKey, meta);
    } catch (e) {
      inner = `<p class="bo-banner is-warn">${esc(e.message)}</p>`;
    }

    body.innerHTML = `
      <header class="bo-head">
        <h2><span class="bo-head-ico uic-tile" aria-hidden="true">${icon(window.utpIcons ? window.utpIcons.slugForSettings(pageKey) : '', 24)}</span>${esc(meta.label || pageKey)}</h2>
        ${meta.description ? `<p class="bo-lead">${esc(meta.description)}</p>` : ''}
        <div class="bo-verbs bo-verbs-lg">${verbBadges(meta)}</div>
      </header>
      ${banner}
      ${meta.protected && meta.delete_semantics_label
        ? `<p class="bo-banner is-note">${esc(t('protected_note', { semantics: meta.delete_semantics_label }))}</p>`
        : ''}
      <div id="boPageBody">${inner}</div>`;
    bindRoutes(body);
    await afterPageRender(pageKey, meta);
  }

  async function settingsPageBody(pageKey, meta) {
    if (!S.settings) {
      try { S.settings = await api('/api/staff/settings'); } catch (_) { S.settings = {}; }
    }
    const s = S.settings || {};
    if (pageKey === 'VAT Settings' && s.vat) return chargeCard('VAT', s.vat, meta);
    if (pageKey === 'Service Charge Settings' && s.service_charge) return chargeCard('SERVICE_CHARGE', s.service_charge, meta);
    if (pageKey === 'Time Zone Settings' && s.timezone) return timezoneCard(s.timezone, meta);
    if (pageKey === 'Ticket Validity Settings' && s.ticket_validity) return validityCard(s.ticket_validity, meta);
    if (pageKey === 'Currency Settings' && s.base_currency) return currencyCard(s.base_currency, meta);
    if (pageKey === 'Exchange Rates' && s.exchange_rates) return ratesCard(s.exchange_rates, meta);
    if (pageKey === 'Payment Type') return await paymentTypesCard(meta);

    // Config-backed pages (operating hours, booking rules, rounding, languages,
    // integrations, …): the overview carries them under config_pages, and each is a
    // single scoped value edited through one generic form driven by its shape.
    const configBlock = (s.config_pages || {})[pageKey];
    // Rounding gets a purpose-built card: method radios, a configurable increment and
    // a live preview (Fix.md §2–§4), rather than the generic key/value form.
    if (pageKey === 'Rounding' && configBlock) return roundingCard(configBlock, meta);
    if (configBlock) return configCard(pageKey, configBlock, meta);

    // Record-collection pages (ticket types, staff, promotions, devices, seats, …):
    // fetch the rows and render a table. Loaded here (not from the overview) so a
    // large table is fetched only when its page is opened.
    if (RECORD_PAGES.indexOf(pageKey) !== -1) {
      try {
        const data = await api('/api/staff/settings/records?page=' + encodeURIComponent(pageKey));
        // The descriptor (if any) tells us which Add/Edit/Delete controls this
        // principal may see and what fields the form needs. Cached for the form
        // handlers that run in afterPageRender.
        S._crud = data.crud || null;
        S._crudRecords = data.records || [];
        return recordTable(pageKey, data.records || [], meta, data.crud || null);
      } catch (e) {
        return `<p class="bo-banner is-warn">${esc(e.message)}</p>`;
      }
    }
    // A page with no known editor is a genuine gap, not a shipped placeholder.
    return `<p class="bo-banner is-warn">${esc(t('no_results'))}</p>`;
  }

  // The pages the settings screen renders as a record table. Kept in sync with the
  // server's /api/staff/settings/records switch.
  const RECORD_PAGES = [
    'Ticket Types', 'Customer Segments', 'Products', 'Experiences', 'Pricing',
    'Promotions', 'Coupon Codes', 'Cash Coupons', 'Member Rewards',
    'Email Templates', 'Gates', 'Access Points',
    'Kiosks', 'POS Devices', 'Printers', 'Gate Devices', 'Devices',
    'Shows', 'Show Schedule', 'Staff', 'Roles', 'Venues', 'Organization', 'Brand',
    'Terms & Conditions', 'Seat Type', 'Seat Zone', 'Seat Layout',
    'Areas', 'Capacity', 'Time Slots', 'Audit Logs', 'Permissions',
  ];

  /* --- VAT / service charge (§29 progressive disclosure, §41 preview) --- */

  function chargeCard(kind, block, meta) {
    const cur = block.current || {};
    const editable = !!block.can_edit;
    const isVat = kind === 'VAT';
    const history = (block.history || []).slice(0, 6);
    return `
      <section class="bo-panel" data-charge="${esc(kind)}">
        <h3 class="bo-panel-h">${esc(t('enable'))}</h3>
        <div class="bo-field">
          <label class="bo-switch">
            <input type="checkbox" id="chEnabled" ${cur.enabled ? 'checked' : ''} ${editable ? '' : 'disabled'}>
            <span>${esc(cur.display_name || (isVat ? 'VAT' : 'Service charge'))}</span>
          </label>
        </div>
        <div class="bo-field">
          <label for="chRate">${esc(t('rate'))}</label>
          <div class="bo-inline">
            <input id="chRate" type="number" min="0" max="100" step="0.01"
                   value="${(Number(cur.rate_bp || 0) / 100).toFixed(2)}" ${editable ? '' : 'disabled'}>
            <span class="bo-suffix">%</span>
          </div>
        </div>
        <fieldset class="bo-field">
          <legend>${esc(isVat ? t('mode_included') + ' / ' + t('mode_excluded') : t('mode_included') + ' / ' + t('mode_excluded'))}</legend>
          <label class="bo-radio"><input type="radio" name="chMode" value="INCLUSIVE"
            ${cur.mode === 'INCLUSIVE' ? 'checked' : ''} ${editable ? '' : 'disabled'}>
            <span><strong>${esc(t('mode_included'))}</strong><small>${esc(t('mode_included_help'))}</small></span></label>
          <label class="bo-radio"><input type="radio" name="chMode" value="EXCLUSIVE"
            ${cur.mode !== 'INCLUSIVE' ? 'checked' : ''} ${editable ? '' : 'disabled'}>
            <span><strong>${esc(t('mode_excluded'))}</strong><small>${esc(t('mode_excluded_help'))}</small></span></label>
        </fieldset>
        <details class="bo-advanced">
          <summary>${esc(t('advanced'))}</summary>
          <div class="bo-field">
            <label for="chFrom">${esc(t('effective_from'))}</label>
            <input id="chFrom" type="date" value="${esc(cur.effective_from ? String(cur.effective_from).slice(0, 10) : '')}"
                   ${editable ? '' : 'disabled'}>
          </div>
          <div class="bo-field">
            <label for="chReason">${esc(t('reason'))}</label>
            <input id="chReason" type="text" ${editable ? '' : 'disabled'}>
            <small class="hint">${esc(t('reason_hint'))}</small>
          </div>
        </details>
        <section class="bo-preview" id="chPreview" aria-live="polite">
          <h3 class="bo-panel-h">${esc(t('preview'))}</h3>
          <p class="bo-loading">${esc(t('loading'))}</p>
        </section>
        ${editable ? `<div class="bo-actions">
          <button type="button" class="ghost" id="chCancel">${esc(t('cancel'))}</button>
          <button type="button" class="primary" id="chSave">${esc(t('save_changes'))}</button>
        </div>` : ''}
        ${history.length ? `<section class="bo-history">
          <h3 class="bo-panel-h">${esc(t('history'))}</h3>
          <ul>${history.map((h) => `<li><span>${esc(String(h.effective_from || '').slice(0, 10))}</span>
            <span>${esc(pct(h.rate_bp))} · ${esc(h.mode === 'INCLUSIVE' ? t('mode_included') : t('mode_excluded'))}</span>
            <span>${h.enabled ? esc(t('yes')) : esc(t('no'))}</span></li>`).join('')}</ul>
        </section>` : ''}
      </section>`;
  }

  async function loadChargePreview() {
    const node = $('chPreview');
    if (!node) return;
    try {
      const data = await api('/api/staff/settings/charge-preview?amount_minor=107000');
      const b = data.breakdown;
      const c = b.currency;
      const rows = [
        [t('price_shown'), money(b.base_minor, c)],
        [t('before_charge'), money(b.taxable_base_minor, c)],
      ];
      if (b.service_charge_minor) rows.push([`${t('sc_amount')} ${pct(b.service_charge_rate_bp)}`, money(b.service_charge_minor, c)]);
      rows.push([`${t('charge_amount')} ${pct(b.vat_rate_bp)}`, money(b.vat_minor, c)]);
      rows.push([t('grand_total'), money(b.grand_total_minor, c)]);
      node.innerHTML = `<h3 class="bo-panel-h">${esc(t('preview'))}</h3>
        <dl class="bo-preview-list">${rows.map(([k, v], i) =>
          `<div class="bo-preview-row${i === rows.length - 1 ? ' is-total' : ''}"><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`
        ).join('')}</dl>
        <p class="hint">${esc(t('preview'))} — ${esc(pageLabel('VAT Settings'))}</p>`;
    } catch (e) {
      node.innerHTML = `<h3 class="bo-panel-h">${esc(t('preview'))}</h3><p class="hint">${esc(e.message)}</p>`;
    }
  }

  /* --- other implemented settings pages --- */

  function timezoneCard(block, meta) {
    const zones = ['Asia/Bangkok', 'Asia/Tokyo', 'Asia/Singapore', 'Asia/Shanghai',
      'Europe/London', 'Europe/Moscow', 'America/New_York', 'Australia/Sydney'];
    const options = zones.indexOf(block.timezone) === -1 ? [block.timezone].concat(zones) : zones;
    return `<section class="bo-panel" data-form="timezone">
      <div class="bo-field">
        <label for="tzValue">${esc(t('timezone'))}</label>
        <select id="tzValue" ${block.can_edit ? '' : 'disabled'}>${options.map((z) =>
          `<option value="${esc(z)}"${z === block.timezone ? ' selected' : ''}>${esc(z)}</option>`).join('')}</select>
        <small class="hint">${esc('Tickets, cutoffs and reports are evaluated in this zone. Already-issued tickets keep the zone they were issued with.')}</small>
      </div>
      <div class="bo-field">
        <label for="tzReason">${esc(t('reason'))}</label>
        <input id="tzReason" type="text" ${block.can_edit ? '' : 'disabled'}>
      </div>
      ${block.can_edit ? `<div class="bo-actions">
        <button type="button" class="ghost" data-route="/settings/business">${esc(t('cancel'))}</button>
        <button type="button" class="primary" id="tzSave">${esc(t('save_changes'))}</button></div>` : ''}
    </section>`;
  }

  function validityCard(block, meta) {
    const p = block.policy || {};
    return `<section class="bo-panel" data-form="validity">
      <div class="bo-field">
        <label for="vtType">${esc(t('validity_type'))}</label>
        <select id="vtType" ${block.can_edit ? '' : 'disabled'}>${(block.validity_types || []).map((v) =>
          `<option value="${esc(v)}"${v === p.validity_type ? ' selected' : ''}>${esc(v)}</option>`).join('')}</select>
      </div>
      <div class="bo-field">
        <label for="vtExpires">${esc(t('expires_at'))}</label>
        <input id="vtExpires" type="text" value="${esc(p.expires_at_local || '23:59:59')}" ${block.can_edit ? '' : 'disabled'}>
      </div>
      <div class="bo-field">
        <label class="bo-switch"><input id="vtReentry" type="checkbox" ${p.reentry_allowed ? 'checked' : ''}
          ${block.can_edit ? '' : 'disabled'}><span>${esc(t('reentry'))}</span></label>
      </div>
      <div class="bo-field">
        <label for="vtMax">${esc(t('max_entries'))}</label>
        <input id="vtMax" type="number" min="1" value="${esc(p.max_entries || 1)}" ${block.can_edit ? '' : 'disabled'}>
      </div>
      <div class="bo-field">
        <label for="vtReason">${esc(t('reason'))}</label>
        <input id="vtReason" type="text" ${block.can_edit ? '' : 'disabled'}>
      </div>
      ${block.can_edit ? `<div class="bo-actions">
        <button type="button" class="ghost" data-route="/settings/booking_ticketing">${esc(t('cancel'))}</button>
        <button type="button" class="primary" id="vtSave">${esc(t('save_changes'))}</button></div>` : ''}
    </section>`;
  }

  function currencyCard(block, meta) {
    const info = block.info || {};
    return `<section class="bo-panel" data-form="currency">
      <div class="bo-field">
        <label for="cyValue">${esc(t('currency'))}</label>
        <input id="cyValue" type="text" maxlength="3" value="${esc(block.currency)}" ${block.can_edit ? '' : 'disabled'}>
        <small class="hint">ISO 4217 · ${esc(String(info.decimals))} ${esc('decimal places')} · ${esc(info.symbol || '')}</small>
      </div>
      <div class="bo-field">
        <label for="cyReason">${esc(t('reason'))}</label>
        <input id="cyReason" type="text" ${block.can_edit ? '' : 'disabled'}>
      </div>
      ${block.can_edit ? `<div class="bo-actions">
        <button type="button" class="ghost" data-route="/settings/pricing_tax">${esc(t('cancel'))}</button>
        <button type="button" class="primary" id="cySave">${esc(t('save_changes'))}</button></div>` : ''}
    </section>`;
  }

  function ratesCard(block, meta) {
    const rates = block.rates || [];
    return `<section class="bo-panel" data-form="rates">
      ${rates.length ? `<ul class="bo-rates">${rates.map((r) => `<li>
        <span class="bo-rate-dir">${esc(t('rate_direction', { from: r.from_currency, rate: r.rate, to: r.to_currency }))}</span>
        <span class="bo-rate-meta">${esc(String(r.effective_from || '').slice(0, 10))} · ${esc(r.status)}</span>
        ${block.can_edit && r.status === 'ACTIVE'
          ? `<button type="button" class="ghost small" data-end-rate="${esc(r.id)}">${esc(t('end_rate'))}</button>`
          : ''}
      </li>`).join('')}</ul>` : `<p class="hint">${esc(t('no_rates'))}</p>`}
      ${block.can_add ? `<div class="bo-rate-add">
        <h3 class="bo-panel-h">${esc(t('add_rate'))}</h3>
        <div class="bo-inline">
          <span>1</span>
          <input id="rtFrom" type="text" maxlength="3" placeholder="USD" size="4">
          <span>=</span>
          <input id="rtRate" type="text" inputmode="decimal" placeholder="33.100000" size="12">
          <input id="rtTo" type="text" maxlength="3" placeholder="THB" size="4">
        </div>
        <div class="bo-field"><label for="rtFromDate">${esc(t('effective_from'))}</label>
          <input id="rtFromDate" type="date"></div>
        <div class="bo-field"><label for="rtReason">${esc(t('reason'))}</label>
          <input id="rtReason" type="text"></div>
        <div class="bo-actions"><button type="button" class="primary" id="rtSave">${esc(t('add_rate'))}</button></div>
      </div>` : ''}
    </section>`;
  }

  // Payment Type has a purpose-built card (its own read endpoint), but it now offers
  // the same Add/Edit/Delete as the generic record pages, reusing the record form and
  // the generic /settings/records endpoints. The descriptor is defined here because
  // Payment Type is read through /api/staff/payment-types, not /settings/records.
  const PAYMENT_TYPE_CRUD = {
    page: 'Payment Type', sensitive: true, full_edit: true, delete_label: 'Archive',
    fields: [
      { name: 'display_name', label: 'Display name', type: 'i18n', required: true, help: 'help_display_name' },
      { name: 'code', label: 'Code', type: 'text', required: true, help: 'help_code' },
      { name: 'method', label: 'Method', type: 'select', required: true, options: [
        { value: 'CARD', label: 'Card' }, { value: 'QR_BANK_TRANSFER', label: 'QR / bank transfer' },
        { value: 'EWALLET', label: 'E-wallet' }, { value: 'CASH', label: 'Cash' },
        { value: 'STORED_VALUE', label: 'Stored value' }] },
      { name: 'display_order', label: 'Display order', type: 'number' },
    ],
  };

  async function paymentTypesCard(meta) {
    let data;
    try { data = await api('/api/staff/payment-types'); } catch (e) { return `<p class="bo-banner is-warn">${esc(e.message)}</p>`; }
    const list = data.payment_types || [];
    S._ptList = list;
    const addBtn = meta.can_add
      ? `<button type="button" class="primary small" data-pt-add="1">+ ${esc(t('add'))}</button>` : '';
    const hasActions = meta.can_edit || meta.can_delete;
    const row = (p) => {
      // Display Name is a localized map — resolve it, never render the object (§14).
      // Code is the stable internal identifier, shown immediately after (§16).
      const edit = meta.can_edit
        ? `<button type="button" class="ghost small" data-pt-edit="${esc(p.id)}">${esc(t('edit'))}</button>` : '';
      const del = meta.can_delete && p.status === 'ACTIVE'
        ? `<button type="button" class="ghost small danger" data-archive-pt="${esc(p.id)}">${esc(meta.delete_semantics_label || t('delete'))}</button>` : '';
      return `<tr>
        <td>${esc(localName(p.display_name, p.code))}</td>
        <td><code>${esc(p.code)}</code></td>
        <td>${esc(p.provider ? localName(p.provider, p.provider) : '—')}</td>
        <td>${esc(paymentChannels(p))}</td>
        <td><span class="st ${statusTone(p.status)}">${esc(p.status)}</span></td>
        ${hasActions ? `<td class="rp-actions">${edit}${del}</td>` : ''}
      </tr>`;
    };
    return `<section class="bo-panel bo-rec" data-form="payment-types">
      <div class="bo-rec-head"><span class="bo-rec-count">${list.length} ${esc(t('records_word'))}</span>${addBtn}</div>
      <div class="rp-table-wrap"><table class="rp-table"><thead><tr>
        <th>${esc(t('display_name_word'))}</th>
        <th>${esc(t('code_word'))}</th>
        <th>${esc(t('provider_word'))}</th>
        <th>${esc(t('channels_word'))}</th>
        <th>${esc(t('status_word'))}</th>
        ${hasActions ? `<th class="rp-actions">${esc(t('actions_word'))}</th>` : ''}
      </tr></thead>
      <tbody>${list.map(row).join('')}</tbody></table></div>
      ${list.length ? '' : `<div class="bo-empty-state"><span class="bo-empty-ico uic-tile" aria-hidden="true"></span><p>${esc(t('no_results'))}</p></div>`}
    </section>`;
  }

  /* --- Rounding settings (Fix.md §1–§7) --- */

  const ROUNDING_METHODS = [
    ['ROUND_UP', 'round_up_label', 'round_up_help'],
    ['ROUND_DOWN', 'round_down_label', 'round_down_help'],
    ['ROUND_HALF_UP', 'round_half_label', 'round_half_help'],
    ['NONE', 'round_none_label', 'round_none_help'],
  ];
  const ROUNDING_INCREMENTS = [
    [1, '0.01'], [5, '0.05'], [10, '0.10'], [25, '0.25'], [50, '0.50'], [100, '1.00'],
  ];

  // Pure integer rounding that mirrors the backend apply_rounding, so the preview a
  // manager sees matches what the server will compute (never floating point).
  function roundPreview(amountMinor, mode, incMinor) {
    if (mode === 'NONE' || !incMinor || incMinor <= 1) return amountMinor;
    const rem = ((amountMinor % incMinor) + incMinor) % incMinor;
    if (rem === 0) return amountMinor;
    if (mode === 'ROUND_UP') return amountMinor - rem + incMinor;
    if (mode === 'ROUND_DOWN') return amountMinor - rem;
    return amountMinor - rem + (rem * 2 >= incMinor ? incMinor : 0); // HALF_UP
  }

  function roundingCard(block, meta) {
    const editable = !!block.can_edit;
    const value = block.value || {};
    const mode = value.mode || 'NONE';
    const inc = value.increment_minor || 100;
    const dis = editable ? '' : 'disabled';
    const inherited = block.inherited
      ? `<p class="bo-banner is-note">${esc(t('inherited_note'))}</p>` : '';
    const methods = ROUNDING_METHODS.map(([val, lab, help]) => `
      <label class="bo-radio"><input type="radio" name="rndMode" value="${val}" ${mode === val ? 'checked' : ''} ${dis}>
        <span><strong>${esc(t(lab))}</strong><small>${esc(t(help))}</small></span></label>`).join('');
    const increments = ROUNDING_INCREMENTS.map(([m, lab]) =>
      `<option value="${m}" ${inc === m ? 'selected' : ''}>${lab}</option>`).join('');
    return `<section class="bo-panel" data-rounding>
      ${inherited}
      <fieldset class="bo-field"><legend>${esc(t('rounding_method'))}</legend>${methods}</fieldset>
      <div class="bo-field" data-inc-wrap>
        <label for="rndInc">${esc(t('rounding_increment'))}</label>
        <select id="rndInc" ${dis}>${increments}</select>
      </div>
      <div class="bo-field"><label for="rndReason">${esc(t('reason_word'))}</label>
        <input id="rndReason" type="text" ${dis}></div>
      <h3 class="bo-panel-h">${esc(t('preview'))}</h3>
      <div class="rp-table-wrap"><table class="rp-table" id="rndPreview"></table></div>
      ${editable ? `<div class="bo-actions">
        <button type="button" class="ghost" id="rndCancel">${esc(t('cancel'))}</button>
        <button type="button" class="primary" id="rndSave">${esc(t('save_changes'))}</button>
      </div>` : `<p class="bo-banner is-note">${esc(t('read_only_banner'))}</p>`}
    </section>`;
  }

  function renderRoundingPreview(body) {
    const mode = (body.querySelector('input[name="rndMode"]:checked') || {}).value || 'NONE';
    const inc = parseInt(($('rndInc') || {}).value || '100', 10);
    // Sample amounts (in minor units) chosen to show below-half, half and above-half.
    const samples = [10010, 10049, 10050, 10075, 10099];
    const rows = samples.map((a) => {
      const out = roundPreview(a, mode, inc);
      return `<tr><td>${(a / 100).toFixed(2)}</td><td class="r">${(out / 100).toFixed(2)}</td></tr>`;
    }).join('');
    const el2 = $('rndPreview');
    if (el2) el2.innerHTML = `<thead><tr><th>${esc(t('round_original'))}</th><th class="r">${esc(t('round_rounded'))}</th></tr></thead><tbody>${rows}</tbody>`;
    // The increment only applies to the directional/mathematical methods.
    const incWrap = body.querySelector('[data-inc-wrap]');
    if (incWrap) incWrap.style.display = (mode === 'NONE') ? 'none' : '';
  }

  /* --- generic config-backed page form (§16, §28, §30) ---
   *
   * These pages are one scoped value. Rather than hand-write a form per page, the
   * value's shape drives the controls: a boolean becomes a switch, a number a number
   * input, an "HH:MM" string a time field, and a small set of known structured
   * values (weekday hours, language list, integration/API/webhook lists) get purpose
   * built sections. The result is a real, saving form — not a raw JSON box, which the
   * brief explicitly forbids for business settings. Credentials show a masked "set"
   * state and a field to replace them; a blank replacement keeps the stored secret.
   */
  function configCard(pageKey, block, meta) {
    const editable = !!block.can_edit;
    const value = block.value || {};
    const dis = editable ? '' : 'disabled';
    const inherited = block.inherited
      ? `<p class="bo-banner is-note">${esc(t('inherited_note'))}</p>` : '';
    let inner;
    // Purpose-built sections for the structured pages; a generic renderer for the rest.
    if (pageKey === 'Operating Hours') inner = operatingHoursForm(value, dis);
    else if (pageKey === 'Languages') inner = languagesForm(value, dis);
    else if (pageKey === 'Integrations') inner = integrationsForm(value, dis);
    else if (pageKey === 'API Configuration') inner = apiClientsForm(value, dis);
    else if (pageKey === 'Webhooks') inner = webhooksForm(value, dis);
    else if (pageKey === 'Partner Benefits') inner = partnersForm(value, dis);
    else if (pageKey === 'Numbering') inner = numberingForm(value, dis);
    else inner = genericFields(value, dis);
    return `<section class="bo-panel" data-config="${esc(pageKey)}" data-sensitive="${block.sensitive ? '1' : ''}">
      ${inherited}
      ${inner}
      <div class="bo-field">
        <label for="cfgReason">${esc(t('reason'))}</label>
        <input id="cfgReason" type="text" ${dis}>
        <small class="hint">${esc(t('reason_hint'))}</small>
      </div>
      ${editable ? `<div class="bo-actions">
        <button type="button" class="ghost" id="cfgCancel">${esc(t('cancel'))}</button>
        <button type="button" class="primary" id="cfgSave">${esc(t('save_changes'))}</button>
      </div>` : `<p class="bo-banner is-info">${esc(t('read_only_banner'))}</p>`}
    </section>`;
  }

  // Re-render just the config panel from an in-progress draft value (after "+ Add"),
  // preserving edit rights and sensitivity, and re-wire it.
  async function renderConfigDraft(pageKey, meta, value) {
    const body = $('boPageBody');
    if (!body) return;
    const block = { value: value, can_edit: true, sensitive: (body.querySelector('[data-config]') || {}).dataset && (body.querySelector('[data-config]').dataset.sensitive === '1') };
    body.innerHTML = configCard(pageKey, block, meta);
    await afterPageRender(pageKey, meta);
  }

  // Humanize a snake_case key into a label. Server-owned enums keep their raw value.
  function humanize(key) {
    return String(key).replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  // Resolve a value that may be a localized name map ({en, th, …}) to a display
  // string in the current language, never "[object Object]". A plain string is
  // returned as-is; a null/empty falls back to the supplied default.
  function localName(value, fallback) {
    if (value === null || value === undefined || value === '') return fallback || '';
    if (typeof value === 'object') {
      return value[lang()] || value.en || value[Object.keys(value)[0]] || (fallback || '');
    }
    return String(value);
  }

  // The channels a payment type is offered on, derived from its enable flags, as a
  // short readable list rather than a raw object.
  function paymentChannels(p) {
    const on = [];
    if (p.web_enabled) on.push('Web');
    if (p.kiosk_enabled) on.push('Kiosk');
    if (p.counter_enabled) on.push('Counter');
    return on.join(', ') || '—';
  }
  const _isTime = (v) => typeof v === 'string' && /^\d{2}:\d{2}$/.test(v);

  // Render a scalar/boolean/number field, tagging it with its data-path so the save
  // collector can rebuild the object. Times get a time input, booleans a switch.
  function scalarField(path, val, dis) {
    const id = 'f_' + path.replace(/[^a-z0-9]/gi, '_');
    if (typeof val === 'boolean') {
      return `<div class="bo-field"><label class="bo-switch">
        <input type="checkbox" data-path="${esc(path)}" data-type="bool" ${val ? 'checked' : ''} ${dis}>
        <span>${esc(humanize(path.split('.').pop()))}</span></label></div>`;
    }
    const type = typeof val === 'number' ? 'number' : (_isTime(val) ? 'time' : 'text');
    const dtype = typeof val === 'number' ? 'number' : (_isTime(val) ? 'time' : 'text');
    return `<div class="bo-field"><label for="${id}">${esc(humanize(path.split('.').pop()))}</label>
      <input id="${id}" type="${type}" data-path="${esc(path)}" data-type="${dtype}"
        value="${esc(val === null || val === undefined ? '' : val)}" ${dis}></div>`;
  }

  function genericFields(value, dis) {
    const parts = [];
    Object.keys(value).forEach((k) => {
      const v = value[k];
      if (v === null || typeof v !== 'object') parts.push(scalarField(k, v, dis));
      else if (!Array.isArray(v)) {
        // one level of nesting (e.g. numbering.booking.prefix)
        parts.push(`<fieldset class="bo-subgroup"><legend>${esc(humanize(k))}</legend>`);
        Object.keys(v).forEach((k2) => {
          if (v[k2] === null || typeof v[k2] !== 'object') parts.push(scalarField(k + '.' + k2, v[k2], dis));
        });
        parts.push(`</fieldset>`);
      }
    });
    return `<div class="bo-config-form">${parts.join('')}</div>`;
  }

  // Collect a generic form back into the object shape the API expects.
  function collectGeneric(root) {
    const out = {};
    root.querySelectorAll('[data-path]').forEach((el) => {
      const path = el.dataset.path;
      const type = el.dataset.type;
      let v;
      if (type === 'bool') v = el.checked;
      else if (type === 'number') v = el.value === '' ? 0 : Number(el.value);
      else v = el.value === '' ? null : el.value;
      const keys = path.split('.');
      let node = out;
      for (let i = 0; i < keys.length - 1; i++) { node[keys[i]] = node[keys[i]] || {}; node = node[keys[i]]; }
      node[keys[keys.length - 1]] = v;
    });
    return out;
  }

  const WEEKDAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

  function operatingHoursForm(value, dis) {
    const days = value.days || {};
    const rows = WEEKDAYS.map((d) => {
      const day = days[d] || {};
      return `<tr data-day="${d}">
        <th scope="row">${esc(t('day_' + d.toLowerCase()) || d)}</th>
        <td><label class="bo-switch"><input type="checkbox" data-day-closed="${d}" ${day.closed ? 'checked' : ''} ${dis}>
          <span>${esc(t('closed'))}</span></label></td>
        <td><input type="time" data-day-field="${d}.open" value="${esc(day.open || '')}" ${dis}></td>
        <td><input type="time" data-day-field="${d}.close" value="${esc(day.close || '')}" ${dis}></td>
        <td><input type="time" data-day-field="${d}.last_admission" value="${esc(day.last_admission || '')}" ${dis}></td>
      </tr>`;
    }).join('');
    return `<table class="bo-hours"><thead><tr>
      <th>${esc(t('weekday'))}</th><th>${esc(t('closed'))}</th>
      <th>${esc(t('open'))}</th><th>${esc(t('close'))}</th><th>${esc(t('last_admission'))}</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
  }

  function collectOperatingHours(root) {
    const days = {};
    WEEKDAYS.forEach((d) => {
      const closed = root.querySelector(`[data-day-closed="${d}"]`);
      const get = (f) => (root.querySelector(`[data-day-field="${d}.${f}"]`) || {}).value || null;
      days[d] = { closed: !!(closed && closed.checked), open: get('open'), close: get('close'), last_admission: get('last_admission') };
    });
    return { days: days };
  }

  const ALL_LANGS = [['en', 'English'], ['th', 'ไทย'], ['zh', '中文'], ['ja', '日本語'], ['ru', 'Русский']];

  function languagesForm(value, dis) {
    const enabled = value.enabled || [];
    const def = value.default || enabled[0];
    return `<div class="bo-config-form">
      <fieldset class="bo-subgroup"><legend>${esc(t('enabled_languages'))}</legend>
        ${ALL_LANGS.map(([c, name]) => `<label class="bo-switch">
          <input type="checkbox" data-lang="${c}" ${enabled.indexOf(c) !== -1 ? 'checked' : ''} ${dis}>
          <span>${esc(name)}</span></label>`).join('')}
      </fieldset>
      <div class="bo-field"><label for="langDefault">${esc(t('default_language'))}</label>
        <select id="langDefault" ${dis}>${ALL_LANGS.map(([c, name]) =>
          `<option value="${c}"${c === def ? ' selected' : ''}>${esc(name)}</option>`).join('')}</select></div>
    </div>`;
  }

  function collectLanguages(root) {
    const enabled = [];
    root.querySelectorAll('[data-lang]').forEach((el) => { if (el.checked) enabled.push(el.dataset.lang); });
    return { enabled: enabled, default: (root.querySelector('#langDefault') || {}).value };
  }

  function numberingForm(value, dis) {
    const docs = ['booking', 'receipt', 'tax_invoice', 'credit_note'];
    return `<div class="bo-config-form">${docs.map((doc) => {
      const row = value[doc] || {};
      return `<fieldset class="bo-subgroup" data-doc="${doc}"><legend>${esc(humanize(doc))}</legend>
        <div class="bo-field"><label>${esc(t('prefix'))}</label>
          <input type="text" data-num="${doc}.prefix" value="${esc(row.prefix || '')}" maxlength="12" ${dis}></div>
        <div class="bo-field"><label>${esc(t('padding'))}</label>
          <input type="number" data-num="${doc}.pad" value="${esc(row.pad || 0)}" min="0" max="12" ${dis}></div>
      </fieldset>`;
    }).join('')}</div>`;
  }

  function collectNumbering(root) {
    const out = {};
    root.querySelectorAll('[data-num]').forEach((el) => {
      const [doc, field] = el.dataset.num.split('.');
      out[doc] = out[doc] || {};
      out[doc][field] = field === 'pad' ? Number(el.value || 0) : el.value;
    });
    return out;
  }

  // A masked-secret field: shows whether a secret is on file and lets the user type a
  // replacement. Left blank, the server keeps the stored one (§secret handling).
  function secretField(path, secretObj, dis) {
    const state = secretObj && secretObj.set
      ? `${esc(t('secret_on_file'))} ••••${esc(secretObj.last4 || '')}` : esc(t('secret_none'));
    return `<div class="bo-field"><label>${esc(t('secret'))}</label>
      <input type="password" data-secret="${esc(path)}" placeholder="${esc(t('secret_leave_blank'))}" autocomplete="new-password" ${dis}>
      <small class="hint">${state}</small></div>`;
  }

  function integrationsForm(value, dis) {
    const names = ['accounting', 'crm', 'marketing'];
    return `<div class="bo-config-form">${names.map((n) => {
      const row = value[n] || {};
      return `<fieldset class="bo-subgroup" data-intg="${n}"><legend>${esc(humanize(n))}</legend>
        <div class="bo-field"><label class="bo-switch">
          <input type="checkbox" data-intg-enabled="${n}" ${row.enabled ? 'checked' : ''} ${dis}>
          <span>${esc(t('enable'))}</span></label></div>
        <div class="bo-field"><label>${esc(t('endpoint'))}</label>
          <input type="url" data-intg-endpoint="${n}" value="${esc(row.endpoint || '')}" placeholder="https://…" ${dis}></div>
        ${secretField('intg.' + n, row.api_key, dis)}
      </fieldset>`;
    }).join('')}</div>`;
  }

  function collectIntegrations(root) {
    const out = {};
    ['accounting', 'crm', 'marketing'].forEach((n) => {
      const en = root.querySelector(`[data-intg-enabled="${n}"]`);
      const ep = root.querySelector(`[data-intg-endpoint="${n}"]`);
      const sec = root.querySelector(`[data-secret="intg.${n}"]`);
      out[n] = { enabled: !!(en && en.checked), endpoint: (ep && ep.value) || null, api_key: (sec && sec.value) || '' };
    });
    return out;
  }

  function apiClientsForm(value, dis) {
    const clients = value.clients || [];
    const rows = clients.map((c, i) => `<fieldset class="bo-subgroup" data-client="${i}">
      <input type="hidden" data-client-id="${i}" value="${esc(c.id || '')}">
      <div class="bo-field"><label>${esc(t('name'))}</label>
        <input type="text" data-client-name="${i}" value="${esc(c.name || '')}" ${dis}></div>
      <div class="bo-field"><label class="bo-switch"><input type="checkbox" data-client-scope-write="${i}"
        ${(c.scopes || []).indexOf('write') !== -1 ? 'checked' : ''} ${dis}><span>${esc(t('scope_write'))}</span></label></div>
      <div class="bo-field"><label class="bo-switch"><input type="checkbox" data-client-active="${i}"
        ${c.status !== 'REVOKED' ? 'checked' : ''} ${dis}><span>${esc(t('active'))}</span></label></div>
      ${secretField('client.' + i, c.key, dis)}
    </fieldset>`).join('');
    return `<div class="bo-config-form" data-list="clients">${rows || `<p class="hint">${esc(t('none_yet'))}</p>`}
      ${dis ? '' : `<button type="button" class="ghost small" data-add-client>+ ${esc(t('add'))}</button>`}</div>`;
  }

  function collectApiClients(root) {
    const clients = [];
    root.querySelectorAll('[data-client]').forEach((fs) => {
      const i = fs.dataset.client;
      const q = (sel) => root.querySelector(sel);
      const scopes = ['read'];
      const w = q(`[data-client-scope-write="${i}"]`); if (w && w.checked) scopes.push('write');
      const active = q(`[data-client-active="${i}"]`);
      const sec = q(`[data-secret="client.${i}"]`);
      clients.push({
        id: (q(`[data-client-id="${i}"]`) || {}).value || null,
        name: (q(`[data-client-name="${i}"]`) || {}).value || '',
        scopes: scopes,
        status: active && active.checked ? 'ACTIVE' : 'REVOKED',
        key: (sec && sec.value) || '',
      });
    });
    return { clients: clients };
  }

  const HOOK_EVENTS = ['booking.confirmed', 'payment.captured', 'ticket.scanned', 'booking.cancelled', 'refund.completed'];

  function webhooksForm(value, dis) {
    const hooks = value.hooks || [];
    const rows = hooks.map((h, i) => `<fieldset class="bo-subgroup" data-hook="${i}">
      <input type="hidden" data-hook-id="${i}" value="${esc(h.id || '')}">
      <div class="bo-field"><label>${esc(t('endpoint'))}</label>
        <input type="url" data-hook-url="${i}" value="${esc(h.url || '')}" placeholder="https://…" ${dis}></div>
      <fieldset class="bo-inline-checks"><legend>${esc(t('events'))}</legend>${HOOK_EVENTS.map((ev) =>
        `<label class="bo-switch"><input type="checkbox" data-hook-event="${i}|${ev}"
          ${(h.events || []).indexOf(ev) !== -1 ? 'checked' : ''} ${dis}><span>${esc(ev)}</span></label>`).join('')}</fieldset>
      <div class="bo-field"><label class="bo-switch"><input type="checkbox" data-hook-active="${i}"
        ${h.status !== 'INACTIVE' ? 'checked' : ''} ${dis}><span>${esc(t('active'))}</span></label></div>
      ${secretField('hook.' + i, h.signing_secret, dis)}
    </fieldset>`).join('');
    return `<div class="bo-config-form" data-list="hooks">${rows || `<p class="hint">${esc(t('none_yet'))}</p>`}
      ${dis ? '' : `<button type="button" class="ghost small" data-add-hook>+ ${esc(t('add'))}</button>`}</div>`;
  }

  function collectWebhooks(root) {
    const hooks = [];
    root.querySelectorAll('[data-hook]').forEach((fs) => {
      const i = fs.dataset.hook;
      const q = (sel) => root.querySelector(sel);
      const events = [];
      root.querySelectorAll(`[data-hook-event^="${i}|"]`).forEach((el) => { if (el.checked) events.push(el.dataset.hookEvent.split('|')[1]); });
      const active = q(`[data-hook-active="${i}"]`);
      const sec = q(`[data-secret="hook.${i}"]`);
      hooks.push({
        id: (q(`[data-hook-id="${i}"]`) || {}).value || null,
        url: (q(`[data-hook-url="${i}"]`) || {}).value || '',
        events: events,
        status: active && active.checked ? 'ACTIVE' : 'INACTIVE',
        signing_secret: (sec && sec.value) || '',
      });
    });
    return { hooks: hooks };
  }

  function partnersForm(value, dis) {
    const partners = value.partners || [];
    const rows = partners.map((p, i) => `<fieldset class="bo-subgroup" data-partner="${i}">
      <div class="bo-field"><label>${esc(t('code'))}</label>
        <input type="text" data-partner-code="${i}" value="${esc(p.code || '')}" ${dis}></div>
      <div class="bo-field"><label>${esc(t('name'))}</label>
        <input type="text" data-partner-name="${i}" value="${esc(p.name || '')}" ${dis}></div>
      <div class="bo-field"><label>${esc(t('discount_pct'))}</label>
        <input type="number" step="0.01" data-partner-discount="${i}" value="${((p.discount_bp || 0) / 100).toFixed(2)}" ${dis}></div>
      <div class="bo-field"><label>${esc(t('commission_pct'))}</label>
        <input type="number" step="0.01" data-partner-commission="${i}" value="${((p.commission_bp || 0) / 100).toFixed(2)}" ${dis}></div>
      <div class="bo-field"><label class="bo-switch"><input type="checkbox" data-partner-active="${i}"
        ${p.status !== 'INACTIVE' ? 'checked' : ''} ${dis}><span>${esc(t('active'))}</span></label></div>
    </fieldset>`).join('');
    return `<div class="bo-config-form" data-list="partners">${rows || `<p class="hint">${esc(t('none_yet'))}</p>`}
      ${dis ? '' : `<button type="button" class="ghost small" data-add-partner>+ ${esc(t('add'))}</button>`}</div>`;
  }

  function collectPartners(root) {
    const partners = [];
    root.querySelectorAll('[data-partner]').forEach((fs) => {
      const i = fs.dataset.partner;
      const q = (sel) => (root.querySelector(sel) || {}).value;
      const active = root.querySelector(`[data-partner-active="${i}"]`);
      partners.push({
        code: q(`[data-partner-code="${i}"]`) || '',
        name: q(`[data-partner-name="${i}"]`) || '',
        discount_bp: Math.round(Number(q(`[data-partner-discount="${i}"]`) || 0) * 100),
        commission_bp: Math.round(Number(q(`[data-partner-commission="${i}"]`) || 0) * 100),
        status: active && active.checked ? 'ACTIVE' : 'INACTIVE',
      });
    });
    return { partners: partners };
  }

  function collectConfigValue(pageKey, root) {
    if (pageKey === 'Operating Hours') return collectOperatingHours(root);
    if (pageKey === 'Languages') return collectLanguages(root);
    if (pageKey === 'Numbering') return collectNumbering(root);
    if (pageKey === 'Integrations') return collectIntegrations(root);
    if (pageKey === 'API Configuration') return collectApiClients(root);
    if (pageKey === 'Webhooks') return collectWebhooks(root);
    if (pageKey === 'Partner Benefits') return collectPartners(root);
    return collectGeneric(root);
  }

  /* --- record-collection table (§14) --- */

  function recordTable(pageKey, records, meta, crud) {
    // A page with a CRUD descriptor gets Add/Edit/Delete controls scoped to this
    // principal's verbs (§14–§17); one without stays a read-only overview whose
    // create/edit forms live in the owning module.
    const canCreate = !!(crud && crud.can_create);
    const canUpdate = !!(crud && crud.can_update);
    const canDelete = !!(crud && crud.can_delete);
    const hasRowActions = canUpdate || canDelete;
    const deleteLabel = (crud && crud.delete_label) || t('delete');

    const addButton = canCreate
      ? `<button type="button" class="primary small" data-rec-add="1">+ ${esc(t('add'))}</button>`
      : '';
    const head = `<div class="bo-rec-head">
        <span class="bo-rec-count">${records.length} ${esc(t('records_word'))}</span>
        ${addButton}
      </div>`;

    if (!records.length) {
      return `<div class="bo-rec">${head}
        <div class="bo-empty-state">
          <span class="bo-empty-ico uic-tile" aria-hidden="true"></span>
          <p>${esc(t('no_records'))}</p>
        </div>
      </div>`;
    }

    // Columns are inferred from the first row, minus noisy id fields; name maps get
    // their English (or first) label.
    const sample = records[0];
    const cols = Object.keys(sample).filter((k) => k !== 'id' && !k.endsWith('_json'));
    const cell = (v) => {
      if (v === null || v === undefined) return '—';
      if (typeof v === 'object') return esc(v.en || v[Object.keys(v)[0]] || JSON.stringify(v));
      if (typeof v === 'boolean') return v ? esc(t('yes')) : esc(t('no'));
      return esc(v);
    };
    // Staff rows carry an extra "Access" control opening the effective-permission
    // viewer (Fix.md Gap 1, §36) — visible whenever the row actions column shows.
    const isStaffPage = pageKey === 'Staff';
    const anyActions = hasRowActions || isStaffPage;
    const rowActions = (r) => {
      if (!anyActions) return '';
      const active = String(r.status || r.state || 'ACTIVE').toUpperCase() === 'ACTIVE';
      const access = isStaffPage
        ? `<button type="button" class="ghost small" data-staff-access="${esc(r.id)}">${esc(t('access_word'))}</button>`
        : '';
      const edit = canUpdate
        ? `<button type="button" class="ghost small" data-rec-edit="${esc(r.id)}">${esc(t('edit'))}</button>`
        : '';
      // Delete/Archive is offered only while the record is still active — a record
      // already archived/inactive has nothing to remove.
      const del = canDelete && active
        ? `<button type="button" class="ghost small danger" data-rec-del="${esc(r.id)}">${esc(deleteLabel)}</button>`
        : '';
      return `<td class="rp-actions">${access}${edit}${del}</td>`;
    };

    return `<div class="bo-rec" data-rec-page="${esc(pageKey)}">
      ${head}
      <div class="rp-table-wrap"><table class="rp-table"><thead><tr>${
        cols.map((c) => `<th>${esc(humanize(c))}</th>`).join('')}${
        anyActions ? `<th class="rp-actions">${esc(t('actions_word'))}</th>` : ''}</tr></thead>
        <tbody>${records.map((r) => `<tr>${cols.map((c) => {
          const isStatus = c === 'status' || c === 'state';
          const v = r[c];
          if (isStatus) return `<td><span class="st ${statusTone(v)}">${esc(v)}</span></td>`;
          return `<td>${cell(v)}</td>`;
        }).join('')}${rowActions(r)}</tr>`).join('')}</tbody></table></div>
      ${crud ? '' : `<p class="hint bo-rec-note">${esc(t('records_managed_elsewhere'))}</p>`}
    </div>`;
  }

  /* --- generic record add/edit form (settingsAndReports §15–§17, §51) ---
   *
   * One dialog serves every record page. The fields come from the page's CRUD
   * descriptor, so a text field, a number, a switch, a dropdown (static options or a
   * server-provided option source) or a small multi-language name box are all drawn
   * from data rather than bespoke HTML. Sensitive pages additionally require a reason,
   * which the server enforces too.
   */
  function recordFieldControl(field, value, options) {
    const id = 'recf_' + field.name;
    const req = field.required ? 'required' : '';
    const val = value === undefined || value === null ? '' : value;
    if (field.type === 'bool') {
      return `<label class="bo-switch"><input type="checkbox" id="${id}" data-recf="${esc(field.name)}"${
        val ? ' checked' : ''}> <span>${esc(field.label)}</span></label>`;
    }
    let control;
    if (field.type === 'select') {
      const opts = field.options || (field.options_source && options && options[field.options_source]) || [];
      control = `<select id="${id}" data-recf="${esc(field.name)}" ${req}>
        <option value="">—</option>
        ${opts.map((o) => `<option value="${esc(o.value)}"${String(o.value) === String(val) ? ' selected' : ''}>${
          esc(o.label)}</option>`).join('')}
      </select>`;
    } else if (field.type === 'textarea') {
      control = `<textarea id="${id}" data-recf="${esc(field.name)}" ${req}>${esc(val)}</textarea>`;
    } else if (field.type === 'i18n') {
      // A small English field is enough for creation; the owning module handles the
      // full multi-language set. Stored as {en: value}.
      const en = (val && typeof val === 'object') ? (val.en || '') : val;
      control = `<input type="text" id="${id}" data-recf="${esc(field.name)}" data-i18n="1" value="${esc(en)}" ${req}>`;
    } else {
      const type = field.type === 'number' ? 'number' : 'text';
      control = `<input type="${type}" id="${id}" data-recf="${esc(field.name)}" value="${esc(val)}" ${req}>`;
    }
    const help = field.help ? `<p class="bo-field-help">${esc(t(field.help) || field.help)}</p>` : '';
    return `<div class="bo-field"><label for="${id}">${esc(field.label)}${
      field.required ? ' <span class="req-star">*</span>' : ''}</label>${control}${help}</div>`;
  }

  function openRecordForm(pageKey, crud, record) {
    const dialog = $('boRecordDialog');
    const isEdit = !!record;
    $('boRecordTitle').textContent = isEdit ? t('edit') + ' · ' + pageKey : t('add') + ' · ' + pageKey;
    const options = crud.options || {};
    // On edit, only fields the record carries are pre-filled; pages without a full
    // field editor (segments, ticket types, promotions) edit status only, so we show
    // a status control in that case.
    let fieldsHtml;
    if (isEdit && !crud.full_edit) {
      fieldsHtml = `<div class="bo-field"><label for="recf_status">${esc(t('status_word'))}</label>
        <select id="recf_status" data-recf="status">
          <option value="ACTIVE">${esc(t('status_active'))}</option>
          <option value="INACTIVE">${esc(t('status_inactive'))}</option>
        </select></div>`;
    } else {
      fieldsHtml = crud.fields.map((f) => recordFieldControl(f, record ? record[f.name] : undefined, options)).join('');
    }
    if (crud.sensitive) {
      fieldsHtml += `<div class="bo-field"><label for="recfReason">${esc(t('reason_word'))} <span class="req-star">*</span></label>
        <input type="text" id="recfReason" data-recf="reason" required></div>`;
    }
    $('boRecordFields').innerHTML = fieldsHtml;
    const err = $('boRecordError');
    err.hidden = true; err.textContent = '';

    return new Promise((resolve) => {
      const form = $('boRecordForm');
      const collect = () => {
        const out = {};
        $('boRecordFields').querySelectorAll('[data-recf]').forEach((el2) => {
          const name = el2.dataset.recf;
          if (el2.type === 'checkbox') out[name] = el2.checked;
          else if (el2.dataset.i18n) out[name] = el2.value ? { en: el2.value } : '';
          else out[name] = el2.value;
        });
        return out;
      };
      const cleanup = () => {
        form.removeEventListener('submit', onSubmit);
        $('boRecordCancel').removeEventListener('click', onCancel);
      };
      const onSubmit = (ev) => {
        ev.preventDefault();
        cleanup(); dialog.close(); resolve(collect());
      };
      const onCancel = () => { cleanup(); dialog.close(); resolve(null); };
      form.addEventListener('submit', onSubmit);
      $('boRecordCancel').addEventListener('click', onCancel);
      dialog.showModal();
    });
  }

  // Wire the Add/Edit/Delete controls a record table may have rendered.
  function wireRecordControls(body, pageKey, records, crud) {
    if (!crud) return;
    const byId = {};
    (records || []).forEach((r) => { byId[r.id] = r; });

    const add = body.querySelector('[data-rec-add]');
    if (add) add.addEventListener('click', async () => {
      const payload = await openRecordForm(pageKey, crud, null);
      if (!payload) return;
      await submit('/api/staff/settings/records', Object.assign({ page: pageKey }, payload));
    });

    body.querySelectorAll('[data-rec-edit]').forEach((b) => b.addEventListener('click', async () => {
      const payload = await openRecordForm(pageKey, crud, byId[b.dataset.recEdit] || {});
      if (!payload) return;
      await submit('/api/staff/settings/records/' + encodeURIComponent(b.dataset.recEdit),
        Object.assign({ page: pageKey }, payload));
    }));

    body.querySelectorAll('[data-rec-del]').forEach((b) => b.addEventListener('click', async () => {
      const label = (crud.delete_label || t('delete'));
      let reason;
      if (crud.sensitive) {
        reason = window.prompt(t('reason_word'));
        if (reason === null) return; // cancelled
      } else {
        const ok = await confirmChange([[t('confirm_to'), label]]);
        if (!ok) return;
      }
      await submit('/api/staff/settings/records/' + encodeURIComponent(b.dataset.recDel) + '/delete',
        { page: pageKey, reason: reason || label + ' from Settings' });
    }));

    // Staff "Access" opens the effective-permission viewer (Fix.md Gap 1).
    body.querySelectorAll('[data-staff-access]').forEach((b) => b.addEventListener('click', () => {
      openStaffAccess(b.dataset.staffAccess);
    }));
  }

  /* --- Staff Access & Permissions viewer (Fix.md Gap 1, §36) --- */

  async function openStaffAccess(staffId) {
    const dialog = $('boStaffAccessDialog');
    const bodyEl = $('boStaffAccessBody');
    bodyEl.innerHTML = `<p class="bo-loading">${esc(t('loading'))}</p>`;
    dialog.showModal();
    await renderStaffAccess(staffId);
    const close = $('boStaffAccessClose');
    close.onclick = () => dialog.close();
  }

  async function renderStaffAccess(staffId) {
    const bodyEl = $('boStaffAccessBody');
    let data;
    try {
      data = await api('/api/staff/permissions/summary?staff_id=' + encodeURIComponent(staffId));
    } catch (e) { bodyEl.innerHTML = `<p class="bo-banner is-warn">${esc(e.message)}</p>`; return; }
    S._staffAccess = { id: staffId, data: data };
    $('boStaffAccessTitle').textContent = (data.display_name || t('access_word')) + ' · ' + t('access_word');

    // Load the assignable roles/venues once, so an administrator can add a role.
    let picker = { roles: [], venues: [] };
    try { picker = await api('/api/staff/assignable-roles'); } catch (_) { /* view-only */ }

    const verbs = ['VIEW', 'ADD', 'EDIT', 'DELETE'];
    const byVenue = data.by_venue || {};
    const venueKeys = Object.keys(byVenue);

    // Current role assignments (from every scope), with a Remove control.
    const assignments = (data.assignments || []).filter((a) => a.status === 'ACTIVE');
    const assignRows = assignments.map((a) => `<tr>
        <td>${esc(a.role_name || a.role_code)}</td>
        <td>${esc(a.scope_type)}${a.scope_id ? ' · ' + esc(a.scope_id) : ''}</td>
        <td class="rp-actions"><button type="button" class="ghost small danger" data-remove-assign="${esc(a.id)}">${esc(t('delete'))}</button></td>
      </tr>`).join('');

    // Effective permissions per venue — the heart of §36: pages × verbs, per venue.
    const venueBlocks = venueKeys.length ? venueKeys.map((vid) => {
      const v = byVenue[vid];
      const pages = v.pages || {};
      const rows = Object.keys(pages).map((pk) => {
        const cells = verbs.map((verb) => {
          if (!(verb in pages[pk])) return `<td class="pv-na">–</td>`;
          return `<td class="${pages[pk][verb] ? 'pv-yes' : 'pv-no'}">${pages[pk][verb] ? '✓' : '✕'}</td>`;
        }).join('');
        return `<tr><td>${esc(pk)}</td>${cells}</tr>`;
      }).join('');
      return `<section class="bo-access-venue">
        <h4>${esc(v.venue_code || vid)} <small>${esc((v.roles || []).join(', '))}</small></h4>
        <div class="rp-table-wrap"><table class="rp-table pv-table"><thead><tr>
          <th>${esc(t('pages_word'))}</th>${verbs.map((x) => `<th>${esc(x)}</th>`).join('')}
        </tr></thead><tbody>${rows}</tbody></table></div>
      </section>`;
    }).join('') : `<p class="hint">${esc(t('no_access_anywhere'))}</p>`;

    const canAssign = picker.roles.length > 0;
    const assignForm = canAssign ? `<div class="bo-access-assign">
        <h4>${esc(t('assign_role'))}</h4>
        <div class="bo-inline">
          <select id="saRole">${picker.roles.map((r) => `<option value="${esc(r.id)}">${esc(r.name)} (${r.authority_level})</option>`).join('')}</select>
          <select id="saVenue"><option value="">${esc(t('all_venues') || 'Venue')}</option>${
            picker.venues.map((v) => `<option value="${esc(v.id)}">${esc(localName(v.name, v.code))}</option>`).join('')}</select>
          <input id="saReason" type="text" placeholder="${esc(t('reason_word'))}">
          <button type="button" class="primary small" id="saAssign">${esc(t('assign_role'))}</button>
        </div>
      </div>` : '';

    bodyEl.innerHTML = `
      <div class="bo-access-section">
        <h4>${esc(t('roles_title'))}</h4>
        <div class="rp-table-wrap"><table class="rp-table"><thead><tr>
          <th>${esc(t('roles_title'))}</th><th>${esc(t('scope_word'))}</th><th class="rp-actions">${esc(t('actions_word'))}</th>
        </tr></thead><tbody>${assignRows || `<tr><td colspan="3" class="hint">${esc(t('no_records'))}</td></tr>`}</tbody></table></div>
        ${assignForm}
      </div>
      <div class="bo-access-section">
        <h4>${esc(t('effective_permissions'))}</h4>
        ${venueBlocks}
      </div>`;

    // Wire remove + assign.
    bodyEl.querySelectorAll('[data-remove-assign]').forEach((b) => b.addEventListener('click', async () => {
      const ok = await confirmChange([[t('confirm_to'), t('delete')]]);
      if (!ok) return;
      try {
        await api('/api/staff/role-assignments/' + encodeURIComponent(b.dataset.removeAssign) + '/remove',
          { method: 'POST', body: JSON.stringify({ reason: 'Removed from Staff access screen' }) });
        toast(t('saved'), 'success');
        await renderStaffAccess(staffId);
      } catch (e) { toast(e.message, 'error'); }
    }));
    const assignBtn = $('saAssign');
    if (assignBtn) assignBtn.addEventListener('click', async () => {
      const body = {
        role_id: $('saRole').value,
        scope_type: $('saVenue').value ? 'VENUE' : 'TENANT',
        scope_id: $('saVenue').value || undefined,
        reason: $('saReason').value || 'Assigned from Staff access screen',
      };
      try {
        await api('/api/staff/staff/' + encodeURIComponent(staffId) + '/roles',
          { method: 'POST', body: JSON.stringify(body) });
        toast(t('saved'), 'success');
        await renderStaffAccess(staffId);
      } catch (e) {
        const perField = e.details && e.details.fields;
        const first = perField && Object.keys(perField).length ? perField[Object.keys(perField)[0]] : null;
        toast(first || e.message, 'error');
      }
    });
  }

  function statusTone(v) {
    const s = String(v || '').toUpperCase();
    if (['ACTIVE', 'PUBLISHED', 'VALID', 'ISSUED'].indexOf(s) !== -1) return 'st-good';
    if (['DRAFT', 'PENDING', 'INACTIVE'].indexOf(s) !== -1) return 'st-warn';
    if (['REVOKED', 'ARCHIVED', 'CANCELLED', 'DEACTIVATED'].indexOf(s) !== -1) return 'st-bad';
    return 'st-neutral';
  }

  /* --- wiring for the implemented editors --- */

  async function afterPageRender(pageKey, meta) {
    const body = $('boPageBody');
    if (!body) return;
    bindRoutes(body);
    const markDirty = () => { S.dirty = { page: pageKey }; };
    body.querySelectorAll('input, select').forEach((f) => f.addEventListener('change', markDirty));

    // Record-collection page with a CRUD descriptor: wire its Add/Edit/Delete
    // controls. The descriptor and rows were cached when the table was built.
    if (S._crud && body.querySelector('[data-rec-page]')) {
      wireRecordControls(body, pageKey, S._crudRecords, S._crud);
    }

    // Rounding settings: live preview + save through the config-page endpoint.
    if (body.querySelector('[data-rounding]')) {
      renderRoundingPreview(body);
      body.querySelectorAll('input[name="rndMode"], #rndInc').forEach((el2) =>
        el2.addEventListener('change', () => renderRoundingPreview(body)));
      const save = $('rndSave');
      if (save) save.addEventListener('click', async () => {
        const mode = (body.querySelector('input[name="rndMode"]:checked') || {}).value || 'NONE';
        const value = { mode: mode };
        if (mode !== 'NONE') value.increment_minor = parseInt(($('rndInc') || {}).value || '100', 10);
        const ok = await confirmChange([[t('confirm_to'), t(({
          ROUND_UP: 'round_up_label', ROUND_DOWN: 'round_down_label',
          ROUND_HALF_UP: 'round_half_label', NONE: 'round_none_label' })[mode])]]);
        if (!ok) return;
        await submit('/api/staff/settings/page',
          { page: 'Rounding', value: value, reason: ($('rndReason') || {}).value || undefined });
      });
      const cancel = $('rndCancel');
      if (cancel) cancel.addEventListener('click', () => { S.dirty = null; handleRoute(); });
      return;
    }

    // Config-backed page: the generic form saves through POST /settings/page. A
    // sensitive page confirms first (§40); the reason, if given, is audited.
    const cfgPanel = body.querySelector('[data-config]');
    if (cfgPanel) {
      // "+ Add" appends a blank row to a list-shaped config value and re-renders,
      // so the new row is immediately editable.
      const appendAndRerender = async (listKey, blank) => {
        const val = collectConfigValue(pageKey, body);
        (val[listKey] = val[listKey] || []).push(blank);
        S._configDraft = { page: pageKey, value: val };
        await renderConfigDraft(pageKey, meta, val);
      };
      const addClient = cfgPanel.querySelector('[data-add-client]');
      if (addClient) addClient.addEventListener('click', () =>
        appendAndRerender('clients', { id: null, name: '', scopes: ['read'], status: 'ACTIVE' }));
      const addHook = cfgPanel.querySelector('[data-add-hook]');
      if (addHook) addHook.addEventListener('click', () =>
        appendAndRerender('hooks', { id: null, url: '', events: [], status: 'ACTIVE' }));
      const addPartner = cfgPanel.querySelector('[data-add-partner]');
      if (addPartner) addPartner.addEventListener('click', () =>
        appendAndRerender('partners', { code: '', name: '', discount_bp: 0, commission_bp: 0, status: 'ACTIVE' }));

      const save = $('cfgSave');
      if (save) {
        save.addEventListener('click', async () => {
          const value = collectConfigValue(pageKey, body);
          const reason = ($('cfgReason') || {}).value || undefined;
          if (cfgPanel.dataset.sensitive === '1') {
            const ok = await confirmChange([[t('confirm_to'), meta.label || pageKey]]);
            if (!ok) return;
          }
          await submit('/api/staff/settings/page', { page: pageKey, value: value, reason: reason });
        });
        const cancel = $('cfgCancel');
        if (cancel) cancel.addEventListener('click', () => { S.dirty = null; S._configDraft = null; handleRoute(); });
      }
      return;
    }

    if (pageKey === 'VAT Settings' || pageKey === 'Service Charge Settings') {
      await loadChargePreview();
      const save = $('chSave');
      if (save) {
        save.addEventListener('click', async () => {
          const block = pageKey === 'VAT Settings' ? S.settings.vat : S.settings.service_charge;
          const cur = block.current || {};
          const rateBp = Math.round(parseFloat($('chRate').value || '0') * 100);
          const mode = (body.querySelector('input[name="chMode"]:checked') || {}).value || 'EXCLUSIVE';
          const enabled = $('chEnabled').checked;
          // §40: a rate that will change what every future guest is charged is not
          // saved on a single click. The dialog states the old and new value.
          const ok = await confirmChange([
            [t('confirm_from'), `${cur.enabled ? pct(cur.rate_bp) : t('no')} · ${cur.mode === 'INCLUSIVE' ? t('mode_included') : t('mode_excluded')}`],
            [t('confirm_to'), `${enabled ? pct(rateBp) : t('no')} · ${mode === 'INCLUSIVE' ? t('mode_included') : t('mode_excluded')}`],
            [t('effective_from'), $('chFrom').value || '—'],
          ]);
          if (!ok) return;
          const path = pageKey === 'VAT Settings' ? '/api/staff/settings/vat' : '/api/staff/settings/service-charge';
          await submit(path, {
            enabled: enabled, rate_bp: rateBp, mode: mode,
            effective_from: $('chFrom').value || undefined,
            reason: $('chReason').value || undefined,
          });
        });
        $('chCancel').addEventListener('click', () => { S.dirty = null; handleRoute(); });
      }
    }
    if ($('tzSave')) {
      $('tzSave').addEventListener('click', async () => {
        const ok = await confirmChange([
          [t('confirm_from'), S.settings.timezone.timezone],
          [t('confirm_to'), $('tzValue').value],
        ]);
        if (!ok) return;
        await submit('/api/staff/settings/timezone', {
          timezone: $('tzValue').value, reason: $('tzReason').value || undefined,
        });
      });
    }
    if ($('vtSave')) {
      $('vtSave').addEventListener('click', async () => {
        const ok = await confirmChange([[t('confirm_to'), $('vtType').value]]);
        if (!ok) return;
        await submit('/api/staff/settings/ticket-validity', {
          validity_type: $('vtType').value,
          expires_at_local: $('vtExpires').value || undefined,
          reentry_allowed: $('vtReentry').checked,
          max_entries: parseInt($('vtMax').value || '1', 10),
          reason: $('vtReason').value || undefined,
        });
      });
    }
    if ($('cySave')) {
      $('cySave').addEventListener('click', async () => {
        const ok = await confirmChange([
          [t('confirm_from'), S.settings.base_currency.currency],
          [t('confirm_to'), $('cyValue').value.toUpperCase()],
        ]);
        if (!ok) return;
        await submit('/api/staff/settings/base-currency', {
          currency: $('cyValue').value.toUpperCase(), reason: $('cyReason').value || undefined,
        });
      });
    }
    if ($('rtSave')) {
      $('rtSave').addEventListener('click', async () => {
        await submit('/api/staff/settings/exchange-rates', {
          from_currency: $('rtFrom').value.toUpperCase(),
          to_currency: $('rtTo').value.toUpperCase(),
          rate: $('rtRate').value,
          effective_from: $('rtFromDate').value || undefined,
          reason: $('rtReason').value || undefined,
        });
      });
    }
    body.querySelectorAll('[data-end-rate]').forEach((b) => b.addEventListener('click', async () => {
      await submit(`/api/staff/settings/exchange-rates/${encodeURIComponent(b.dataset.endRate)}/end`, {
        reason: 'Ended from the Settings screen',
      });
    }));
    // Payment Type add/edit reuse the generic record form + endpoints.
    const ptAdd = body.querySelector('[data-pt-add]');
    if (ptAdd) ptAdd.addEventListener('click', async () => {
      const payload = await openRecordForm('Payment Type', PAYMENT_TYPE_CRUD, null);
      if (!payload) return;
      await submit('/api/staff/settings/records', Object.assign({ page: 'Payment Type' }, payload));
    });
    body.querySelectorAll('[data-pt-edit]').forEach((b) => b.addEventListener('click', async () => {
      const rec = (S._ptList || []).filter((p) => p.id === b.dataset.ptEdit)[0] || {};
      const payload = await openRecordForm('Payment Type', PAYMENT_TYPE_CRUD, rec);
      if (!payload) return;
      await submit('/api/staff/settings/records/' + encodeURIComponent(b.dataset.ptEdit),
        Object.assign({ page: 'Payment Type' }, payload));
    }));
    body.querySelectorAll('[data-archive-pt]').forEach((b) => b.addEventListener('click', async () => {
      // §51: DELETE on a payment type used in history means disable, and the
      // confirmation says which. The server enforces the same rule.
      const reason = window.prompt(t('reason_word'));
      if (reason === null) return;
      await submit('/api/staff/settings/records/' + encodeURIComponent(b.dataset.archivePt) + '/delete',
        { page: 'Payment Type', reason: reason || 'Archived from Settings' });
    }));
  }

  async function submit(path, payload) {
    try {
      await api(path, { method: 'POST', body: JSON.stringify(payload) });
      S.dirty = null;
      S.settings = null;
      toast(t('saved'), 'success');
      await handleRoute();
    } catch (e) {
      // The server's message is shown as sent. A refusal here is the real control
      // working: the button was visible, the request was still rejected (§75).
      const perField = e.details && e.details.fields;
      const first = perField && Object.keys(perField).length ? perField[Object.keys(perField)[0]] : null;
      toast(first || e.message, 'error');
    }
  }

  function confirmChange(rows) {
    return new Promise((resolve) => {
      const dialog = $('boConfirmDialog');
      $('boConfirmTitle').textContent = t('confirm_title');
      $('boConfirmBody').innerHTML = rows.map(([k, v]) =>
        `<div class="bo-confirm-row"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('');
      $('boConfirmCancel').textContent = t('cancel');
      $('boConfirmOk').textContent = t('confirm_ok');
      const done = (value) => {
        dialog.close();
        $('boConfirmOk').removeEventListener('click', ok);
        $('boConfirmCancel').removeEventListener('click', no);
        resolve(value);
      };
      const ok = () => done(true);
      const no = () => done(false);
      $('boConfirmOk').addEventListener('click', ok);
      $('boConfirmCancel').addEventListener('click', no);
      dialog.showModal();
    });
  }

  /* --------------------------------------------- role editor (§19, §20, §21) */

  async function renderRoles() {
    crumb([{ label: t('settings_title'), route: '/settings' }, { label: t('roles_title') }]);
    const body = $('boBody');
    body.innerHTML = `<p class="bo-loading">${esc(t('loading'))}</p>`;
    if (!S.matrix) S.matrix = await api('/api/staff/permissions/matrix');
    const matrix = S.matrix;

    // Group the registry the way the editor needs it: collapsible sections, never one
    // flat list of 291 checkboxes (§19).
    const byGroup = {};
    matrix.pages.forEach((p) => {
      if (!byGroup[p.group]) byGroup[p.group] = { label: p.group_label, rows: [] };
      byGroup[p.group].rows.push(p);
    });
    const actionsByGroup = {};
    matrix.actions.forEach((a) => {
      if (!actionsByGroup[a.group]) actionsByGroup[a.group] = { label: a.group_label, rows: [] };
      actionsByGroup[a.group].rows.push(a);
    });

    const verbs = matrix.verbs;
    body.innerHTML = `
      <header class="bo-head">
        <h2>${esc(t('roles_title'))}</h2>
        <p class="bo-lead">${esc(t('roles_lead'))}</p>
      </header>
      <p class="bo-banner is-info">${esc(t('role_saving_disabled'))}</p>
      <div class="bo-role-tools">
        <label class="sr-only" for="boRoleSearch">${esc(t('search_ph'))}</label>
        <input id="boRoleSearch" type="search" placeholder="${esc(t('search_ph'))}" autocomplete="off">
        <button type="button" class="ghost small" id="boExpandAll">${esc(t('expand_all'))}</button>
        <button type="button" class="ghost small" id="boCollapseAll">${esc(t('collapse_all'))}</button>
      </div>
      <div id="boMatrix" class="bo-matrix">
        <h3 class="bo-panel-h">${esc(t('pages_word'))}</h3>
        ${Object.keys(byGroup).sort().map((group) => matrixGroup(byGroup[group], verbs)).join('')}
        <h3 class="bo-panel-h">${esc(t('actions_word'))}</h3>
        ${Object.keys(actionsByGroup).sort().map((group) => actionGroup(actionsByGroup[group])).join('')}
      </div>
      <div id="boRoleSummary"></div>`;

    $('boExpandAll').addEventListener('click', () =>
      body.querySelectorAll('#boMatrix details').forEach((d) => { d.open = true; }));
    $('boCollapseAll').addEventListener('click', () =>
      body.querySelectorAll('#boMatrix details').forEach((d) => { d.open = false; }));
    $('boRoleSearch').addEventListener('input', () => {
      const q = $('boRoleSearch').value.trim().toLowerCase();
      body.querySelectorAll('#boMatrix [data-search]').forEach((row) => {
        row.hidden = !!q && row.dataset.search.indexOf(q) === -1;
      });
      if (q) body.querySelectorAll('#boMatrix details').forEach((d) => { d.open = true; });
    });
    renderRoleSummary();
  }

  function matrixGroup(group, verbs) {
    return `<details class="bo-mgroup" open>
      <summary>${esc(group.label)}<span class="bo-count">${group.rows.length}</span></summary>
      <table class="bo-mtable">
        <thead><tr><th scope="col">${esc(t('pages_word'))}</th>${
          verbs.map((v) => `<th scope="col">${esc(v.label)}</th>`).join('')}</tr></thead>
        <tbody>${group.rows.map((row) => `<tr data-search="${esc((row.label + ' ' + row.page).toLowerCase())}">
          <th scope="row"><span class="bo-mpage">${esc(row.label)}</span>${
            row.protected ? `<span class="bo-pill">${esc(row.delete_semantics_label || '')}</span>` : ''}</th>
          ${verbs.map((v) => {
            if (!row.verbs[v.verb]) {
              // §13's "-": rendering an unchecked box here would invite a grant the
              // registry would silently drop.
              return `<td class="is-na" title="${esc(t('not_applicable'))}">–</td>`;
            }
            const held = can(row.page, v.verb);
            return `<td><span class="bo-check${held ? ' is-on' : ''}" role="img"
              aria-label="${esc(v.label)}: ${esc(held ? t('yes') : t('no'))}"></span></td>`;
          }).join('')}
        </tr>`).join('')}</tbody>
      </table>
    </details>`;
  }

  function actionGroup(group) {
    return `<details class="bo-mgroup">
      <summary>${esc(group.label)}<span class="bo-count">${group.rows.length}</span></summary>
      <ul class="bo-alist">${group.rows.map((a) => `<li data-search="${esc((a.label + ' ' + a.key).toLowerCase())}">
        <span class="bo-check${canAction(a.key) ? ' is-on' : ''}" role="img"
          aria-label="${esc(a.label)}: ${esc(canAction(a.key) ? t('yes') : t('no'))}"></span>
        <span class="bo-amain"><strong>${esc(a.label)}</strong>
          ${a.description ? `<small>${esc(a.description)}</small>` : ''}</span>
        ${a.requires_reason ? `<span class="bo-pill">${esc(t('reason'))}</span>` : ''}
        ${a.revenue_affecting ? '<span class="bo-pill is-warn">฿</span>' : ''}
      </li>`).join('')}</ul>
    </details>`;
  }

  // §21: the pre-save summary, in counts and plain language rather than 291 states.
  function renderRoleSummary(summary) {
    const node = $('boRoleSummary');
    if (!node) return;
    const s = summary || (S.profile && S.profile.summary);
    if (!s) { node.innerHTML = ''; return; }
    node.innerHTML = `
      <section class="bo-summary">
        <h3 class="bo-panel-h">${esc(t('summary_title'))}</h3>
        <div class="bo-summary-grid">
          <div><strong>${s.pages_viewable}</strong><small>${esc(t('can_view'))}</small></div>
          <div><strong>${s.can_add}</strong><small>${esc(t('can_add'))}</small></div>
          <div><strong>${s.can_edit}</strong><small>${esc(t('can_edit'))}</small></div>
          <div><strong>${s.can_delete}</strong><small>${esc(t('can_delete'))}</small></div>
        </div>
        <h4 class="bo-summary-h">${esc(t('sensitive'))}</h4>
        <ul class="bo-sensitive">${s.sensitive.map((row) => `<li>
          <span>${esc(row.label)}</span><span class="bo-pill${row.level === 'NONE' ? '' : ' is-on'}">${
            esc(levelLabel(row.level))}</span></li>`).join('')}</ul>
        ${s.warnings && s.warnings.length ? `<h4 class="bo-summary-h">${esc(t('warnings'))}</h4>
          <ul class="bo-warnings">${s.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
      </section>`;
  }

  function levelLabel(level) {
    if (level === 'NONE') return t('no_access');
    if (level === 'READ_ONLY') return t('read_only');
    if (level === 'FULL') return `${t('can_delete')}`;
    if (level === 'EDIT') return t('can_edit');
    if (level === 'ADD') return t('can_add');
    return level;
  }

  /* ------------------------------------------- my effective permissions (§36) */

  async function renderMyAccess() {
    crumb([{ label: t('settings_title'), route: '/settings' }, { label: t('eff_title') }]);
    const body = $('boBody');
    body.innerHTML = `<p class="bo-loading">${esc(t('loading'))}</p>`;
    let data;
    try { data = await api('/api/staff/permissions/summary'); }
    catch (e) { body.innerHTML = `<p class="bo-banner is-warn">${esc(e.message)}</p>`; return; }
    body.innerHTML = `
      <header class="bo-head">
        <h2>${esc(t('eff_title'))}</h2>
        <p class="bo-lead">${esc(t('eff_lead'))}</p>
      </header>
      <div class="bo-eff">
        <p class="bo-eff-meta">${esc((data.roles || []).join(', '))} · ${esc(String(data.authority_level))}</p>
        <ul class="bo-eff-cats">${(data.settings || []).map((c) => `<li>
          <strong>${esc(c.label)}</strong>
          <span>${c.pages.map((p) => esc(p.label)).join(', ')}</span></li>`).join('')}</ul>
      </div>
      <div id="boRoleSummary"></div>`;
    renderRoleSummary(data.summary);
  }

  /* ------------------------------------------------------------------- boot */

  function wire() {
    $('loginForm').addEventListener('submit', submitLogin);
    $('loginReveal').addEventListener('click', () => {
      const input = $('loginPass');
      const shown = input.type === 'text';
      input.type = shown ? 'password' : 'text';
      $('loginReveal').setAttribute('aria-pressed', shown ? 'false' : 'true');
      $('loginReveal').title = shown ? t('login_show') : t('login_hide');
      $('loginRevealText').textContent = shown ? t('login_show') : t('login_hide');
      input.focus();
    });
    // Self-service password reset (settings spec §1). "Forgot password?" swaps the
    // sign-in card for the reset card; request a code by email, then set a new
    // password with it. All server-enforced (enumeration-safe, token, policy).
    const showLogin = () => { $('loginForm').hidden = false; $('resetForm').hidden = true; };
    const showReset = () => {
      $('loginForm').hidden = true; $('resetForm').hidden = false;
      $('resetStep2').hidden = true; $('resetSubmit').hidden = true; $('resetRequest').hidden = false;
      $('resetNotice').hidden = true;
      const em = $('loginEmail').value.trim();
      if (em) $('resetEmail').value = em;
    };
    $('loginForgot').addEventListener('click', showReset);
    $('resetBack').addEventListener('click', showLogin);
    const resetNote = (msg, isErr) => {
      const n = $('resetNotice'); n.textContent = msg; n.hidden = false;
      n.classList.toggle('is-error', !!isErr);
    };
    $('resetRequest').addEventListener('click', async () => {
      const email = $('resetEmail').value.trim();
      if (!email) { resetNote(t('err_email'), true); return; }
      try {
        const r = await api('/api/staff/forgot-password',
          { method: 'POST', body: JSON.stringify({ email: email }) });
        // Reveal step 2. In local dev the server returns demo_token so the flow can be
        // completed without a real mailbox; a production build omits it.
        $('resetStep2').hidden = false;
        $('resetRequest').hidden = true;
        $('resetSubmit').hidden = false;
        if (r.demo_token) {
          $('resetToken').value = r.demo_token;
          resetNote(t('reset_code_sent') + ' ' + t('reset_demo_prefix') + ' ' + r.demo_token);
        } else {
          resetNote(r.message || t('reset_code_sent'));
        }
      } catch (e) { resetNote(e.message, true); }
    });
    $('resetSubmit').addEventListener('click', async () => {
      const email = $('resetEmail').value.trim();
      const token = $('resetToken').value.trim();
      const credential = $('resetPass').value;
      if (!token || !credential) { resetNote(t('reset_need_all'), true); return; }
      try {
        await api('/api/staff/reset-password',
          { method: 'POST', body: JSON.stringify({ email: email, token: token, credential: credential }) });
        showLogin();
        const n = $('loginNotice'); n.textContent = t('reset_done'); n.hidden = false;
        $('loginEmail').value = email; $('loginPass').value = '';
        $('loginPass').focus();
      } catch (e) {
        const perField = e.details && e.details.fields;
        const first = perField && Object.keys(perField).length ? perField[Object.keys(perField)[0]] : null;
        resetNote(first || e.message, true);
      }
    });
    $('boBurger').addEventListener('click', () => {
      const open = document.body.classList.toggle('bo-side-open');
      $('boBurger').setAttribute('aria-expanded', open ? 'true' : 'false');
    });

    window.addEventListener('hashchange', handleRoute);

    // A session that dies anywhere — expiry, revocation, sign-out in another tab —
    // must not leave a protected screen on display (§57).
    auth.onChange((token, reason) => {
      if (token) return;
      const onProtected = !$('view-backoffice').hidden || !$('view-reports').hidden || !$('view-ops').hidden;
      if (reason === 'session-expired') {
        S.profile = null;
        S.notice = t('session_expired');
        if (onProtected) { S.intended = parseRoute(location.hash).path; go('/login', { replace: true }); }
      }
    });
  }

  wire();
  // Only take over the view when the URL actually asks for one. A plain visit to "/"
  // is a customer arriving at the booking page, and must stay that way.
  if (location.hash && location.hash.length > 2) handleRoute();

  window.utpBackoffice = {
    go: navigate,
    handleRoute: handleRoute,
    can: can,
    state: S,
    // Called when the customer language selector changes. The server localizes page
    // and category names, so the profile has to be re-fetched rather than re-labelled
    // locally — otherwise the sidebar would keep the previous language's page names.
    relabel: function () {
      if (!$('view-login').hidden) { applyLoginText(); return; }
      if ($('view-backoffice').hidden) return;
      S.profileAt = 0;
      S.matrix = null;
      handleRoute();
    },
  };
})();
