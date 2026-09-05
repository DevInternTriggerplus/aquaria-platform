/* Aquaria Phuket booking client.
 *
 * No framework, and no business logic. Prices, availability, promotions, consent items
 * and totals all come from the API, because the server is authoritative (R42.1) — the
 * client's job is presentation and nothing else.
 *
 * CSP is nonce-based with no unsafe-inline, so there are no inline handlers anywhere:
 * every listener is attached here.
 */
'use strict';

const state = {
  csrf: null,
  lang: 'en',              // customer language (persisted in localStorage)
  venue: null,
  products: [],
  selectedDate: null,
  pricingGroup: null,      // 'LOCAL' | 'INTL' — Thai vs International
  quantities: {},          // ticket_type_id -> qty
  quote: null,
  promoCodes: [],
  consent: null,
  holdTimer: null,
  holdExpiresAt: null,
  paymentTypes: [],
  paymentTypeId: null,
  paymentMethod: null,
  staffToken: null,
  showDate: null,
  showFilter: 'ALL',
  lastBooking: null,
  settings: null,
};

/* Minimal string table for UI strings this client introduces (update spec §1).
 * The server localizes calendar/shows/consent/payment/error text; these are only
 * the handful of labels rendered purely on the client. English is the fallback. */
const LANG_STORAGE_KEY = 'utp_lang';
const SUPPORTED_LANGS = ['en', 'th', 'zh', 'ja', 'ru'];
const T_STRINGS = {
  en: {
    group_prompt: 'Who are the tickets for?', group_local: 'Thai', group_intl: 'International',
    group_local_note: 'Thai residents & qualifying expatriates', group_intl_note: 'Visitors from outside Thailand',
    make_payment: 'Make a Payment', pay: 'Pay {amount}', total: 'Total',
    review_title: 'Review & pay', sec_visit: 'Your visit', sec_visitors: 'Visitors & tickets',
    sec_promos: 'Promotions', sec_price: 'Price summary', sec_details: 'Your details',
    sec_payment: 'Payment method', edit: 'Edit',
    subtotal: 'Subtotal', discount: 'Discount', service_charge: 'Service charge', vat: 'VAT',
    rounding: 'Rounding', included: '(included)', no_payment: 'No payment methods are available right now.',
    seats_reserved: 'Seats reserved for {time}', choose_group: 'Choose Thai or International to see prices.',
    session: 'Session', before_you_pay: 'Before you pay',
    your_order: 'Your order', date_label: 'Date', venue_label: 'Venue', not_selected: 'Not selected',
    hero_title: 'Book your visit to {venue}',
    hero_lead: 'Pick your date, choose your tickets and pay securely. Your QR e-ticket arrives by email — walk straight to the gate.',
    step_date: 'Choose your visit date', step_tickets: 'Choose your tickets', step_details: 'Your details',
    promo_code: 'Promotion code', optional: '(optional)', apply: 'Apply',
    full_name: 'Full name', email_address: 'Email address', mobile_number: 'Mobile number',
    email_help: 'Your e-ticket and QR code are sent here.', back: 'Back',
    booking_confirmed: 'Booking confirmed', view_email: 'View the email we sent', book_another: 'Book another visit',
    complete_details: 'Please complete your details.', choose_tickets_first: 'Choose your tickets first.',
    enter_name: 'Enter the name for the booking.', enter_email: 'Enter a valid email address so we can send your ticket.',
    max_per_booking: 'Up to {max} of this ticket per booking.',
    hold_expired: 'Your reservation time ran out. Please confirm your tickets again.',
    could_not_book: 'We could not complete your booking.',
    booking_done_toast: 'Booking confirmed. Your ticket is on its way.',
    payment_selected: '{name} selected',
    date_selected: 'Selected {date}.',
    done_summary: 'Booking {number} · {count} ticket(s) · {amount} paid. Your e-ticket has been emailed to {email}.',
    tk_ticket: 'Ticket', tk_visit: 'Visit date', tk_valid: 'Valid until', tk_entries: 'Entries',
    tk_type: 'Type', tk_state: 'Status', tk_qr_alt: 'Entrance QR code for ticket {number}',
    view_eticket: 'Open the e-ticket',
    print_eticket: 'Print e-ticket', print_thermal: 'Print gate ticket (80mm)',
    print_blocked: 'Your browser blocked the print window. Allow pop-ups for this site and try again.',
    tk_unlimited: 'unlimited',
    accept_and_pay: 'Accept and pay', processing: 'Processing…',
    required: 'Required', optional_label: 'Optional', lawful_basis: 'Lawful basis',
    language: 'Language', nav_book: 'Book', nav_shows: "What's on", nav_manage: 'My booking',
    nav_reports: 'Reports', nav_backoffice: 'Back office', nav_ops: 'Operations',
    online_booking: 'Online booking', fact_qr: 'Instant QR e-ticket', fact_no_account: 'No account needed',
    hours_full: 'Open {open}\u2013{close} \u00b7 last admission {last} \u00b7 {tz}', hours_short: 'Open {open}\u2013{close}',
    cal_few_left: 'Few left', cal_full: 'Full', cal_closed: 'Closed', cal_soon: 'Soon',
    nothing_scheduled: 'Nothing scheduled.', next_shows: 'Next shows', diff_location: 'different location today',
    show_reservation_required: 'Reservation required', show_included: 'Included with your ticket',
    show_in_min: 'in {n} min', show_min: '{n} min',
    manage_title: 'Manage your booking', manage_hint: "We'll email a one-time code to the address on the booking.",
    manage_booking_number: 'Booking number', manage_email: 'Email address', manage_send_code: 'Send me a code',
    manage_code: 'Verification code', manage_view: 'View my booking',
  },
  th: {
    group_prompt: 'ซื้อบัตรสำหรับใคร?', group_local: 'คนไทย', group_intl: 'ต่างชาติ',
    group_local_note: 'คนไทยและชาวต่างชาติที่พำนักในไทย', group_intl_note: 'ผู้เยี่ยมชมจากต่างประเทศ',
    make_payment: 'ชำระเงิน', pay: 'ชำระ {amount}', total: 'รวม',
    review_title: 'ตรวจสอบและชำระเงิน', sec_visit: 'การเข้าชมของคุณ', sec_visitors: 'ผู้เข้าชมและบัตร',
    sec_promos: 'โปรโมชั่น', sec_price: 'สรุปราคา', sec_details: 'ข้อมูลของคุณ',
    sec_payment: 'วิธีชำระเงิน', edit: 'แก้ไข',
    subtotal: 'ยอดรวมย่อย', discount: 'ส่วนลด', service_charge: 'ค่าบริการ', vat: 'ภาษีมูลค่าเพิ่ม',
    rounding: 'การปัดเศษ', included: '(รวมแล้ว)', no_payment: 'ขณะนี้ยังไม่มีวิธีชำระเงินให้เลือก',
    seats_reserved: 'สำรองที่นั่งไว้ {time}', choose_group: 'เลือกคนไทยหรือต่างชาติเพื่อดูราคา',
    session: 'รอบ', before_you_pay: 'ก่อนชำระเงิน',
    your_order: 'รายการของคุณ', date_label: 'วันที่', venue_label: 'สถานที่', not_selected: 'ยังไม่ได้เลือก',
    hero_title: 'จองบัตรเข้าชม {venue}',
    hero_lead: 'เลือกวันที่ เลือกบัตร และชำระเงินอย่างปลอดภัย รับ QR e-ticket ทางอีเมล เข้าประตูได้ทันที',
    step_date: 'เลือกวันที่เข้าชม', step_tickets: 'เลือกบัตร', step_details: 'ข้อมูลของคุณ',
    promo_code: 'รหัสโปรโมชั่น', optional: '(ไม่บังคับ)', apply: 'ใช้รหัส',
    full_name: 'ชื่อ-นามสกุล', email_address: 'อีเมล', mobile_number: 'เบอร์โทรศัพท์',
    email_help: 'เราจะส่ง e-ticket และ QR code ไปที่อีเมลนี้', back: 'ย้อนกลับ',
    booking_confirmed: 'ยืนยันการจองแล้ว', view_email: 'ดูอีเมลที่เราส่ง', book_another: 'จองรอบใหม่',
    complete_details: 'กรุณากรอกข้อมูลให้ครบถ้วน', choose_tickets_first: 'กรุณาเลือกบัตรก่อน',
    enter_name: 'กรุณากรอกชื่อสำหรับการจอง', enter_email: 'กรุณากรอกอีเมลที่ถูกต้องเพื่อรับบัตร',
    max_per_booking: 'จองบัตรนี้ได้สูงสุด {max} ใบต่อการจอง',
    hold_expired: 'หมดเวลาสำรองที่นั่งแล้ว กรุณายืนยันบัตรอีกครั้ง',
    could_not_book: 'ไม่สามารถทำการจองให้เสร็จสมบูรณ์ได้',
    booking_done_toast: 'ยืนยันการจองแล้ว บัตรของคุณกำลังจัดส่ง',
    payment_selected: 'เลือก {name} แล้ว',
    date_selected: 'เลือก {date} แล้ว',
    done_summary: 'การจอง {number} · {count} ใบ · ชำระแล้ว {amount} ส่ง e-ticket ไปที่ {email} แล้ว',
    tk_ticket: 'บัตร', tk_visit: 'วันเข้าชม', tk_valid: 'ใช้ได้ถึง', tk_entries: 'จำนวนเข้า',
    tk_type: 'ประเภท', tk_state: 'สถานะ', tk_qr_alt: 'QR code เข้าชมสำหรับบัตร {number}',
    view_eticket: 'เปิดบัตรอิเล็กทรอนิกส์',
    print_eticket: 'พิมพ์บัตรอิเล็กทรอนิกส์', print_thermal: 'พิมพ์บัตรเข้าชม (80 มม.)',
    print_blocked: 'เบราว์เซอร์บล็อกหน้าต่างการพิมพ์ กรุณาอนุญาตป๊อปอัปสำหรับเว็บไซต์นี้แล้วลองอีกครั้ง',
    tk_unlimited: 'ไม่จำกัด',
    accept_and_pay: 'ยอมรับและชำระเงิน', processing: 'กำลังดำเนินการ…',
    required: 'จำเป็น', optional_label: 'ไม่บังคับ', lawful_basis: 'ฐานทางกฎหมาย',
    language: 'ภาษา', nav_book: 'จองบัตร', nav_shows: 'รอบการแสดง', nav_manage: 'การจองของฉัน',
    nav_reports: 'รายงาน', nav_backoffice: 'ระบบหลังบ้าน', nav_ops: 'สำหรับเจ้าหน้าที่',
    online_booking: 'จองออนไลน์', fact_qr: 'บัตร QR อิเล็กทรอนิกส์ทันที', fact_no_account: 'ไม่ต้องสมัครสมาชิก',
    hours_full: 'เปิด {open}\u2013{close} \u00b7 เข้าชมได้ถึง {last} \u00b7 {tz}', hours_short: 'เปิด {open}\u2013{close}',
    cal_few_left: 'เหลือน้อย', cal_full: 'เต็ม', cal_closed: 'ปิด', cal_soon: 'เร็ว ๆ นี้',
    nothing_scheduled: 'ยังไม่มีรอบการแสดง', next_shows: 'รอบถัดไป', diff_location: 'เปลี่ยนสถานที่วันนี้',
    show_reservation_required: 'ต้องจองล่วงหน้า', show_included: 'รวมอยู่ในบัตรของคุณ',
    show_in_min: 'อีก {n} นาที', show_min: '{n} นาที',
    manage_title: 'จัดการการจองของคุณ', manage_hint: 'เราจะส่งรหัสยืนยันแบบใช้ครั้งเดียวไปยังอีเมลที่ใช้จอง',
    manage_booking_number: 'หมายเลขการจอง', manage_email: 'อีเมล', manage_send_code: 'ส่งรหัสให้ฉัน',
    manage_code: 'รหัสยืนยัน', manage_view: 'ดูการจองของฉัน',
  },
  zh: {
    group_prompt: '门票适用于谁？', group_local: '泰国居民', group_intl: '国际游客',
    group_local_note: '泰国居民及符合条件的侨民', group_intl_note: '来自泰国以外的游客',
    make_payment: '前往付款', pay: '支付 {amount}', total: '合计',
    review_title: '核对并付款', sec_visit: '您的行程', sec_visitors: '游客与门票',
    sec_promos: '促销', sec_price: '价格明细', sec_details: '您的信息',
    sec_payment: '付款方式', edit: '编辑',
    subtotal: '小计', discount: '折扣', service_charge: '服务费', vat: '增值税',
    rounding: '尾数调整', included: '(已包含)', no_payment: '目前没有可用的付款方式。',
    seats_reserved: '座位保留 {time}', choose_group: '请选择泰国居民或国际游客以查看价格。',
    session: '场次', before_you_pay: '付款前须知',
    your_order: '您的订单', date_label: '日期', venue_label: '地点', not_selected: '未选择',
    hero_title: '预订 {venue} 门票',
    hero_lead: '选择日期与门票，安全付款。QR 电子票将发送至您的邮箱，可直接入场。',
    step_date: '选择参观日期', step_tickets: '选择门票', step_details: '您的信息',
    promo_code: '优惠码', optional: '(选填)', apply: '应用',
    full_name: '姓名', email_address: '电子邮箱', mobile_number: '手机号码',
    email_help: '您的电子票和二维码将发送到此邮箱。', back: '返回',
    booking_confirmed: '预订成功', view_email: '查看我们发送的邮件', book_another: '再次预订',
    complete_details: '请填写完整信息。', choose_tickets_first: '请先选择门票。',
    enter_name: '请填写预订人姓名。', enter_email: '请填写有效的电子邮箱以便接收门票。',
    max_per_booking: '此门票每单最多 {max} 张。',
    hold_expired: '座位保留时间已到，请重新确认门票。',
    could_not_book: '我们无法完成您的预订。',
    booking_done_toast: '预订成功，门票即将送达。',
    payment_selected: '已选择 {name}',
    date_selected: '已选择 {date}。',
    done_summary: '预订 {number} · {count} 张 · 已支付 {amount}。电子票已发送至 {email}。',
    tk_ticket: '门票', tk_visit: '参观日期', tk_valid: '有效期至', tk_entries: '入场次数',
    tk_type: '类型', tk_state: '状态', tk_qr_alt: '门票 {number} 的入场二维码',
    view_eticket: '打开电子门票',
    print_eticket: '打印电子门票', print_thermal: '打印入场票（80毫米）',
    print_blocked: '浏览器拦截了打印窗口。请允许本站弹出窗口后重试。',
    tk_unlimited: '不限',
    accept_and_pay: '同意并付款', processing: '处理中…',
    required: '必填', optional_label: '选填', lawful_basis: '法律依据',
    language: '语言', nav_book: '订票', nav_shows: '演出信息', nav_manage: '我的订单',
    nav_reports: '报表', nav_backoffice: '后台管理', nav_ops: '运营看板',
    online_booking: '在线订票', fact_qr: '即时二维码电子票', fact_no_account: '无需注册账户',
    hours_full: '开放 {open}\u2013{close} \u00b7 最后入场 {last} \u00b7 {tz}', hours_short: '开放 {open}\u2013{close}',
    cal_few_left: '余票不多', cal_full: '售罄', cal_closed: '闭馆', cal_soon: '即将开售',
    nothing_scheduled: '暂无演出安排', next_shows: '下一场演出', diff_location: '今日更换地点',
    show_reservation_required: '需要预约', show_included: '门票已包含',
    show_in_min: '{n} 分钟后', show_min: '{n} 分钟',
    manage_title: '管理您的订单', manage_hint: '我们会向订单预留的邮箱发送一次性验证码。',
    manage_booking_number: '订单号', manage_email: '电子邮箱', manage_send_code: '发送验证码',
    manage_code: '验证码', manage_view: '查看我的订单',
  },
  ja: {
    group_prompt: 'チケットの対象は？', group_local: 'タイ居住者', group_intl: '海外からの方',
    group_local_note: 'タイ居住者および対象となる在住外国人', group_intl_note: 'タイ国外からの来場者',
    make_payment: 'お支払いへ進む', pay: '{amount} を支払う', total: '合計',
    review_title: '確認とお支払い', sec_visit: 'ご来場', sec_visitors: '来場者とチケット',
    sec_promos: 'プロモーション', sec_price: '料金明細', sec_details: 'お客様情報',
    sec_payment: 'お支払い方法', edit: '編集',
    subtotal: '小計', discount: '割引', service_charge: 'サービス料', vat: '消費税',
    rounding: '端数調整', included: '(込み)', no_payment: '現在ご利用いただける支払い方法はありません。',
    seats_reserved: '座席を確保中 {time}', choose_group: '料金を表示するにはタイ居住者か海外からの方を選択してください。',
    session: 'セッション', before_you_pay: 'お支払いの前に',
    your_order: 'ご注文内容', date_label: '日付', venue_label: '会場', not_selected: '未選択',
    hero_title: '{venue} のチケットを予約',
    hero_lead: '日付とチケットを選んで安全にお支払い。QR 電子チケットをメールでお送りします。そのままゲートへ。',
    step_date: '来場日を選ぶ', step_tickets: 'チケットを選ぶ', step_details: 'お客様情報',
    promo_code: 'プロモーションコード', optional: '(任意)', apply: '適用',
    full_name: 'お名前', email_address: 'メールアドレス', mobile_number: '携帯番号',
    email_help: 'e チケットと QR コードはこちらに送信されます。', back: '戻る',
    booking_confirmed: '予約完了', view_email: '送信したメールを見る', book_another: 'もう一度予約する',
    complete_details: '必要な情報をご入力ください。', choose_tickets_first: '先にチケットをお選びください。',
    enter_name: '予約者のお名前をご入力ください。', enter_email: 'チケット送信のため有効なメールアドレスをご入力ください。',
    max_per_booking: 'このチケットは 1 回のご予約につき最大 {max} 枚までです。',
    hold_expired: '座席の確保時間が終了しました。もう一度チケットをご確認ください。',
    could_not_book: 'ご予約を完了できませんでした。',
    booking_done_toast: '予約が完了しました。チケットをお送りします。',
    payment_selected: '{name} を選択しました',
    date_selected: '{date} を選択しました。',
    done_summary: '予約 {number} · {count} 枚 · {amount} お支払い済み。e チケットを {email} に送信しました。',
    tk_ticket: 'チケット', tk_visit: '来場日', tk_valid: '有効期限', tk_entries: '入場回数',
    tk_type: '種別', tk_state: 'ステータス', tk_qr_alt: 'チケット {number} の入場QRコード',
    view_eticket: 'Eチケットを開く',
    print_eticket: 'Eチケットを印刷', print_thermal: '入場券を印刷（80mm）',
    print_blocked: 'ブラウザが印刷ウィンドウをブロックしました。ポップアップを許可して再度お試しください。',
    tk_unlimited: '無制限',
    accept_and_pay: '同意してお支払い', processing: '処理中…',
    required: '必須', optional_label: '任意', lawful_basis: '法的根拠',
    language: '言語', nav_book: '予約', nav_shows: '公演情報', nav_manage: '予約の確認',
    nav_reports: 'レポート', nav_backoffice: 'バックオフィス', nav_ops: '運営',
    online_booking: 'オンライン予約', fact_qr: 'QR電子チケットを即時発行', fact_no_account: 'アカウント登録不要',
    hours_full: '開館 {open}\u2013{close} \u00b7 最終入場 {last} \u00b7 {tz}', hours_short: '開館 {open}\u2013{close}',
    cal_few_left: '残りわずか', cal_full: '満員', cal_closed: '休館', cal_soon: '近日発売',
    nothing_scheduled: '予定されている公演はありません', next_shows: '次の公演', diff_location: '本日は場所変更',
    show_reservation_required: '要予約', show_included: 'チケットに含まれます',
    show_in_min: 'あと {n} 分', show_min: '{n} 分',
    manage_title: '予約の管理', manage_hint: '予約時のメールアドレスにワンタイムコードをお送りします。',
    manage_booking_number: '予約番号', manage_email: 'メールアドレス', manage_send_code: 'コードを送信',
    manage_code: '確認コード', manage_view: '予約を表示',
  },
  ru: {
    group_prompt: 'Для кого билеты?', group_local: 'Резиденты Таиланда', group_intl: 'Иностранцы',
    group_local_note: 'Резиденты Таиланда и подходящие экспаты', group_intl_note: 'Гости из-за пределов Таиланда',
    make_payment: 'Перейти к оплате', pay: 'Оплатить {amount}', total: 'Итого',
    review_title: 'Проверка и оплата', sec_visit: 'Ваш визит', sec_visitors: 'Посетители и билеты',
    sec_promos: 'Акции', sec_price: 'Итоги по цене', sec_details: 'Ваши данные',
    sec_payment: 'Способ оплаты', edit: 'Изменить',
    subtotal: 'Подытог', discount: 'Скидка', service_charge: 'Сервисный сбор', vat: 'НДС',
    rounding: 'Округление', included: '(включено)', no_payment: 'Сейчас нет доступных способов оплаты.',
    seats_reserved: 'Места забронированы на {time}', choose_group: 'Выберите резидентов или иностранцев, чтобы увидеть цены.',
    session: 'Сеанс', before_you_pay: 'Перед оплатой',
    your_order: 'Ваш заказ', date_label: 'Дата', venue_label: 'Место', not_selected: 'Не выбрано',
    hero_title: 'Забронируйте визит в {venue}',
    hero_lead: 'Выберите дату и билеты и оплатите безопасно. QR-билет придёт на почту — проходите сразу к воротам.',
    step_date: 'Выберите дату визита', step_tickets: 'Выберите билеты', step_details: 'Ваши данные',
    promo_code: 'Промокод', optional: '(необязательно)', apply: 'Применить',
    full_name: 'Полное имя', email_address: 'Электронная почта', mobile_number: 'Номер телефона',
    email_help: 'Ваш электронный билет и QR-код придут сюда.', back: 'Назад',
    booking_confirmed: 'Бронирование подтверждено', view_email: 'Посмотреть отправленное письмо', book_another: 'Забронировать ещё',
    complete_details: 'Пожалуйста, заполните ваши данные.', choose_tickets_first: 'Сначала выберите билеты.',
    enter_name: 'Введите имя для бронирования.', enter_email: 'Введите действительный адрес почты, чтобы мы отправили билет.',
    max_per_booking: 'Не более {max} таких билетов на одно бронирование.',
    hold_expired: 'Время брони истекло. Пожалуйста, подтвердите билеты снова.',
    could_not_book: 'Не удалось завершить бронирование.',
    booking_done_toast: 'Бронирование подтверждено. Ваш билет уже в пути.',
    payment_selected: '{name} выбрано',
    date_selected: 'Выбрано {date}.',
    done_summary: 'Бронирование {number} · {count} билет(ов) · оплачено {amount}. Электронный билет отправлен на {email}.',
    tk_ticket: 'Билет', tk_visit: 'Дата визита', tk_valid: 'Действует до', tk_entries: 'Входы',
    tk_type: 'Тип', tk_state: 'Статус', tk_qr_alt: 'QR-код входа для билета {number}',
    view_eticket: 'Открыть электронный билет',
    print_eticket: 'Печать электронного билета', print_thermal: 'Печать билета (80 мм)',
    print_blocked: 'Браузер заблокировал окно печати. Разрешите всплывающие окна и попробуйте снова.',
    tk_unlimited: 'без ограничений',
    accept_and_pay: 'Принять и оплатить', processing: 'Обработка…',
    required: 'Обязательно', optional_label: 'Необязательно', lawful_basis: 'Правовое основание',
    language: 'Язык', nav_book: 'Билеты', nav_shows: 'Программа', nav_manage: 'Мои билеты',
    nav_reports: 'Отчёты', nav_backoffice: 'Бэк-офис', nav_ops: 'Операции',
    online_booking: 'Онлайн-бронирование', fact_qr: 'Мгновенный QR-билет', fact_no_account: 'Без регистрации',
    hours_full: 'Открыто {open}\u2013{close} \u00b7 последний вход {last} \u00b7 {tz}', hours_short: 'Открыто {open}\u2013{close}',
    cal_few_left: 'Мало мест', cal_full: 'Мест нет', cal_closed: 'Закрыто', cal_soon: 'Скоро',
    nothing_scheduled: 'Ничего не запланировано', next_shows: 'Ближайшие шоу', diff_location: 'сегодня другое место',
    show_reservation_required: 'Нужна бронь', show_included: 'Входит в ваш билет',
    show_in_min: 'через {n} мин', show_min: '{n} мин',
    manage_title: 'Управление бронированием', manage_hint: 'Мы отправим одноразовый код на адрес, указанный в брони.',
    manage_booking_number: 'Номер брони', manage_email: 'Электронная почта', manage_send_code: 'Отправить код',
    manage_code: 'Код подтверждения', manage_view: 'Показать бронь',
  },
};

function t(key, vars) {
  const table = T_STRINGS[state.lang] || T_STRINGS.en;
  let s = (table && table[key] != null) ? table[key] : (T_STRINGS.en[key] != null ? T_STRINGS.en[key] : key);
  if (vars) Object.keys(vars).forEach((k) => { s = s.replace('{' + k + '}', vars[k]); });
  return s;
}

const $ = (id) => document.getElementById(id);

// Map the app's language to a BCP-47 locale so dates and month names follow the
// chosen language, not the browser's. Falls back to the language code itself.
const LOCALE_BY_LANG = { en: 'en-GB', th: 'th-TH', zh: 'zh-CN', ja: 'ja-JP', ru: 'ru-RU' };
const localeFor = () => LOCALE_BY_LANG[state.lang] || state.lang || 'en';
// Shared with reports.js so the analytics screens format dates and money in the
// same language the guest-facing app is using, and so both use one API helper —
// which is what carries the staff bearer token and the CSRF header.
window.utpLocale = localeFor;
window.utpApi = (path, options) => api(path, options);
// The back office (web/backoffice.js) needs the same four things: the API helper,
// the language, the view switcher and the session token. Exporting them keeps a
// single source of truth for each — two modules holding their own copy of the
// signed-in token is how one of them ends up using a revoked one.
window.utpLang = () => state.lang;
window.utpShowView = (name) => showView(name);

const fmtDate = (iso) => new Date(iso + 'T00:00:00').toLocaleDateString(localeFor(),
  { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });

function money(minor, currency) {
  const value = (minor || 0) / 100;
  try {
    return new Intl.NumberFormat(localeFor(), { style: 'currency', currency: currency || 'THB' }).format(value);
  } catch (_) {
    return `${currency || 'THB'} ${value.toFixed(2)}`;
  }
}

function toast(message, kind) {
  const el = $('toast');
  el.textContent = message;
  el.dataset.kind = kind || 'info';
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 5200);
}

/* --------------------------------------------------------------- staff auth
 *
 * The session token is held in sessionStorage, not localStorage and not only in
 * memory. In memory alone, a reload signed the operator out — which looks like a
 * bug and trains people to keep a second tab open. localStorage would outlive the
 * browser session, leaving a back-office credential on a shared counter machine
 * after the window closed. sessionStorage is scoped to the tab and cleared when it
 * closes, which is the behaviour a staff terminal wants.
 *
 * The token is still only half the story: it is useless without the server, which
 * re-checks the session and re-resolves permissions on every request (§46, §75).
 */
const STAFF_TOKEN_KEY = 'utp_staff_token';
const authListeners = [];

function readStoredToken() {
  try { return sessionStorage.getItem(STAFF_TOKEN_KEY) || null; } catch (_) { return null; }
}

function writeStoredToken(token) {
  try {
    if (token) sessionStorage.setItem(STAFF_TOKEN_KEY, token);
    else sessionStorage.removeItem(STAFF_TOKEN_KEY);
  } catch (_) { /* private mode: fall back to memory only */ }
}

function notifyAuth(reason) {
  authListeners.forEach((fn) => { try { fn(state.staffToken, reason); } catch (_) {} });
}

const utpAuth = {
  get token() { return state.staffToken; },
  set(token) { state.staffToken = token || null; writeStoredToken(state.staffToken); notifyAuth('signed-in'); },
  clear(reason) {
    state.staffToken = null;
    state.settings = null;
    writeStoredToken(null);
    notifyAuth(reason || 'signed-out');
  },
  onChange(fn) { if (typeof fn === 'function') authListeners.push(fn); },
};
state.staffToken = readStoredToken();
window.utpAuth = utpAuth;
window.utpToast = (message, kind) => toast(message, kind);

/* ------------------------------------------------------------------ api */

async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers);
  if (opts.method && opts.method !== 'GET') {
    if (!state.csrf) await loadCsrf();
    opts.headers['X-CSRF-Token'] = state.csrf;
  }
  const isStaff = path.startsWith('/api/staff/');
  if (state.staffToken && isStaff) {
    opts.headers['Authorization'] = `Bearer ${state.staffToken}`;
  }
  // Localize customer-facing requests: the server reads ?lang= to localize calendar,
  // shows, consent, payment-type and error text (update spec §1). Harmless on staff
  // and infra paths, but we keep those untouched to avoid surprises.
  let url = path;
  if (!isStaff && !path.startsWith('/api/csrf') && state.lang && state.lang !== 'en') {
    url += (path.indexOf('?') === -1 ? '?' : '&') + 'lang=' + encodeURIComponent(state.lang);
  }
  const response = await fetch(url, opts);
  const text = await response.text();
  let payload = {};
  if (text) { try { payload = JSON.parse(text); } catch (_) { payload = { raw: text }; } }
  if (!response.ok) {
    const err = payload.error || {};
    const error = new Error(err.message || 'Something went wrong. Please try again.');
    error.code = err.code;
    error.status = response.status;
    error.details = err.details || {};
    error.reference = err.reference;
    // A staff request refused for authentication means the session is gone —
    // expired, revoked, or logged out in another tab. Drop the dead token here, at
    // the one place every request passes through, so no screen keeps retrying with
    // it and the operator gets "please sign in again" instead of a silent failure
    // (§57). Authorization denials are left alone: the session is fine, this
    // particular thing is not allowed (§6).
    if (isStaff && response.status === 401 && state.staffToken) {
      utpAuth.clear('session-expired');
    }
    throw error;
  }
  return payload;
}

async function loadCsrf() {
  const data = await fetch('/api/csrf', { credentials: 'same-origin' }).then((r) => r.json());
  state.csrf = data.csrf_token;
}

/* ------------------------------------------------------------------ nav */

function showView(name) {
  document.querySelectorAll('.view').forEach((v) => { v.hidden = v.id !== `view-${name}`; });
  // The hero belongs to the booking page only; the shows, manage and staff views go
  // straight to their content.
  const hero = $('hero');
  if (hero) hero.hidden = name !== 'book';
  document.querySelectorAll('.nav-btn').forEach((b) => {
    const active = b.dataset.view === name;
    b.classList.toggle('is-active', active);
    b.setAttribute('aria-current', active ? 'page' : 'false');
  });
  // The customer chrome is noise on a back-office screen, and the staff language is
  // chosen per account rather than per visit, so the guest header collapses while a
  // protected view is open.
  document.body.classList.toggle('is-backoffice', name === 'backoffice' || name === 'login');
  if (name === 'shows' && !state.showDate) loadShows(todayIso());
  // Reporting lives in its own module (web/reports.js) and loads on first open.
  if (name === 'reports' && window.utpReports) window.utpReports.open();
}

function todayIso() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

/* ------------------------------------------------------------------ steps */

const STEP_LABELS = ['Date', 'Tickets', 'Your details', 'Confirmed'];

function renderSteps(current) {
  $('steps').innerHTML = STEP_LABELS.map((label, i) => {
    const n = i + 1;
    const st = n < current ? 'done' : n === current ? 'current' : 'todo';
    return `<li data-state="${st}"><span class="n">${n < current ? '\u2713' : n}</span>${label}</li>`;
  }).join('');
}

// Steps: 1 date, 2 tickets, 3 details, 4 confirmed. There is no separate review
// step any more — Review & pay is a modal reached from "Make a Payment" (§18).
function gotoStep(n) {
  renderSteps(n);
  const done = n === 4;
  $('panel-date').hidden = done;
  $('panel-tickets').hidden = done || n < 2;
  $('panel-details').hidden = done || n < 3;
  $('panel-done').hidden = !done;
}

/* ------------------------------------------------------------------ venue */

// Pick a localized value from a {lang: text} object returned by the server, falling
// back to English then any available value (mirrors the server's own fallback, R69.5).
function pick(obj, fallback) {
  if (obj == null) return fallback || '';
  if (typeof obj === 'string') return obj;
  if (obj[state.lang]) return obj[state.lang];
  if (obj.en) return obj.en;
  const first = Object.keys(obj)[0];
  return first ? obj[first] : (fallback || '');
}

async function loadVenue() {
  state.venue = await api('/api/venue');
  const hours = (state.venue.operating_hours || {}).default || {};
  const name = pick(state.venue.name, state.venue.code);
  $('venueName').textContent = name;
  renderVenueChrome(name, hours);
}

// Hero, summary ticket-head and footer all read from the venue record, so a different
// tenant/venue re-skins the page from configuration alone (R1.4, R63.4).
function renderVenueChrome(name, hours) {
  const venue = state.venue || {};
  hours = hours || (venue.operating_hours || {}).default || {};
  // Header hours line, refreshed here so a language switch re-localizes it too.
  $('venueHours').textContent = hours.open
    ? t('hours_full', {
        open: hours.open, close: hours.close,
        last: hours.last_admission, tz: venue.timezone,
      })
    : (venue.timezone || '');
  $('heroTitle').textContent = t('hero_title', { venue: name });
  $('heroLead').textContent = t('hero_lead');
  $('heroHours').textContent = hours.open
    ? t('hours_short', { open: hours.open, close: hours.close })
    : (venue.timezone || '');
  // Ticket head. /api/venue exposes name, address, contact and hours — no marketing
  // copy — so the head shows the venue's real name over its locality line.
  $('sumPlace').textContent = name;
  const tagEl = $('sumTag');
  const locality = shortLocality(venue.address);
  tagEl.textContent = locality;
  tagEl.hidden = !locality;
  const parts = [name, venue.address, hours.open ? `${hours.open}\u2013${hours.close}` : null]
    .filter(Boolean);
  $('footerLine').textContent = parts.join(' \u00b7 ');
}

// The tail of a postal address is the part a guest recognizes ("Mueang Phuket,
// Phuket"). Addresses are free text, so this only trims — it never invents.
function shortLocality(address) {
  if (!address || typeof address !== 'string') return '';
  const parts = address.split(',').map((p) => p.trim()).filter(Boolean);
  if (parts.length <= 1) return address.trim();
  return parts.slice(-2).join(', ');
}

/* ------------------------------------------------------------------ calendar */

let calMonth = null;

// Weekday header, Monday-first, in the chosen language's short weekday names. Derived
// from Intl so it follows the locale rather than a hardcoded English row.
function renderDayOfWeekHeader() {
  const dow = $('calDow');
  if (!dow) return;
  const fmt = new Intl.DateTimeFormat(localeFor(), { weekday: 'short' });
  // 2024-01-01 is a Monday; take seven consecutive days from it.
  const labels = [];
  for (let i = 0; i < 7; i += 1) {
    labels.push(fmt.format(new Date(2024, 0, 1 + i)));
  }
  dow.innerHTML = labels.map((l) => `<span>${escapeHtml(l)}</span>`).join('');
}

async function loadCalendar(monthStart) {
  const start = monthStart || new Date();
  calMonth = new Date(start.getFullYear(), start.getMonth(), 1);
  const last = new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 0);
  const from = iso(calMonth);
  const to = iso(last);
  $('calLabel').textContent = calMonth.toLocaleDateString(localeFor(), { month: 'long', year: 'numeric' });
  renderDayOfWeekHeader();

  let data;
  try {
    data = await api(`/api/calendar?from=${from}&to=${to}`);
  } catch (e) { toast(e.message, 'error'); return; }

  const byDate = {};
  data.cells.forEach((c) => { byDate[c.date] = c; });

  const grid = $('calGrid');
  grid.innerHTML = '';
  const offset = (calMonth.getDay() + 6) % 7;   // Monday-first
  for (let i = 0; i < offset; i++) {
    const blank = document.createElement('div');
    blank.className = 'cal-day empty';
    grid.appendChild(blank);
  }
  for (let d = 1; d <= last.getDate(); d++) {
    const date = iso(new Date(calMonth.getFullYear(), calMonth.getMonth(), d));
    const cell = byDate[date];
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cal-day';
    if (!cell) {
      btn.dataset.state = 'PAST';
      btn.disabled = true;
      btn.innerHTML = `<span class="d">${d}</span>`;
    } else {
      btn.dataset.state = cell.state;
      btn.dataset.today = String(!!cell.is_today);
      btn.disabled = !cell.selectable;
      btn.setAttribute('aria-pressed', String(state.selectedDate === date));
      // Two independent cues plus a full text alternative (R7.2, R7.3).
      btn.setAttribute('aria-label', cell.accessible_label);
      btn.title = cell.accessible_label;
      const mark = cell.is_today ? 'Today' : shortLabel(cell.state);
      btn.innerHTML = `<span class="d">${d}</span><span class="m">${mark}</span>`;
      btn.addEventListener('click', () => {
        if (!cell.selectable) { toast(cell.reason || cell.label, 'error'); return; }
        pickDate(date);
      });
    }
    grid.appendChild(btn);
  }

  $('calLegend').innerHTML = data.legend
    .filter((l) => ['AVAILABLE', 'LIMITED', 'SOLD_OUT', 'CLOSED', 'NOT_YET_ON_SALE'].includes(l.state))
    .map((l) => `<li><span class="sw" style="background:${l.colour}"></span>${l.label}</li>`).join('');
}

function shortLabel(stateName) {
  const key = { LIMITED: 'cal_few_left', SOLD_OUT: 'cal_full', CLOSED: 'cal_closed',
    BLACKOUT: 'cal_closed', NOT_YET_ON_SALE: 'cal_soon' }[stateName];
  return key ? t(key) : '';
}

const iso = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);

async function pickDate(date) {
  state.selectedDate = date;
  state.quantities = {};
  state.quote = null;
  clearHold();
  $('dateHint').textContent = t('date_selected', { date: fmtDate(date) });
  document.querySelectorAll('.cal-day').forEach((b) => b.setAttribute('aria-pressed', 'false'));
  await loadProducts(date);
  gotoStep(2);
  renderSummary();
  loadCalendar(calMonth);
  $('panel-tickets').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ------------------------------------------------------------------ tickets */

/* Minimal, elegant inline SVG icons for the customer segments (§9). Stroke-based in
 * the peacock palette — not emoji, not cartoon. Chosen by the ticket-type code suffix
 * (…-ADULT, …-CHILD, …-SENIOR); anything else gets a neutral person mark. */
const SEG_ICONS = {
  ADULT:
    '<svg viewBox="0 0 48 48" class="seg-svg" aria-hidden="true" focusable="false">'
    + '<circle cx="17" cy="12" r="5"/><path d="M17 18c-4 0-7 3-7 7v13h5v-9"/><path d="M17 29v9"/>'
    + '<circle cx="32" cy="12" r="5"/><path d="M32 18c-4 0-7 3-7 7 0 0 2 1 4 1l-1 12h4l1-8 1 8h4l-1-12c2 0 4-1 4-1 0-4-3-7-7-7"/>'
    + '</svg>',
  CHILD:
    '<svg viewBox="0 0 48 48" class="seg-svg" aria-hidden="true" focusable="false">'
    + '<circle cx="24" cy="13" r="5"/><path d="M24 19c-4 0-6 3-6 6v7h3v9"/>'
    + '<path d="M24 19c4 0 6 3 6 6v7h-3v9"/><path d="M21 41h6"/></svg>',
  SENIOR:
    '<svg viewBox="0 0 48 48" class="seg-svg" aria-hidden="true" focusable="false">'
    + '<circle cx="21" cy="12" r="5"/><path d="M21 18c-4 0-7 3-7 7v6h4l1 11h4"/>'
    + '<path d="M25 25l4 3"/><path d="M33 20v22"/></svg>',
  DEFAULT:
    '<svg viewBox="0 0 48 48" class="seg-svg" aria-hidden="true" focusable="false">'
    + '<circle cx="24" cy="13" r="5"/><path d="M24 19c-5 0-8 3-8 8v11h16V27c0-5-3-8-8-8z"/></svg>',
};

function segmentOf(code) {
  const c = String(code || '').toUpperCase();
  if (c.endsWith('-ADULT') || c.indexOf('ADULT') !== -1) return 'ADULT';
  if (c.endsWith('-CHILD') || c.indexOf('CHILD') !== -1) return 'CHILD';
  if (c.endsWith('-SENIOR') || c.indexOf('SENIOR') !== -1) return 'SENIOR';
  return 'DEFAULT';
}

// The localized display name for a ticket-type code, looked up from the products
// already loaded for the current language. The quote/booking lines carry only the
// code, so the summary and review would otherwise show a raw code like
// "GA-INTL-ADULT". Falls back to the code when the type is not found.
function ttNameByCode(code) {
  for (const product of state.products || []) {
    for (const tt of product.ticket_types || []) {
      if (tt.code === code) return pick(tt.name, tt.code);
    }
  }
  return code;
}

// Which product a pricing group maps to. Aquaria models this as two products; the
// platform stays generic, so we simply match by product code (GA-LOCAL / GA-INTL) and
// fall back to a name/code substring when a venue codes them differently.
function productForGroup(group) {
  if (!state.products.length) return null;
  const wantLocal = group === 'LOCAL';
  const byCode = state.products.find((p) => {
    const c = String(p.code || '').toUpperCase();
    return wantLocal ? c.indexOf('LOCAL') !== -1 : c.indexOf('INTL') !== -1;
  });
  if (byCode) return byCode;
  // Only one product on sale? Use it for either group rather than showing nothing.
  return state.products.length === 1 ? state.products[0] : null;
}

async function loadProducts(date) {
  const data = await api(`/api/products?date=${date}`);
  state.products = data.products || [];
  renderPricingGroup();
  renderProducts();
}

// The Thai / International segmented control. Prices are hidden until a group is
// chosen (§7). The chosen group persists in state for the session.
function renderPricingGroup() {
  const fieldset = $('pricingGroup');
  const available = state.products.length > 0;
  fieldset.hidden = !available;
  $('groupPrompt').textContent = t('group_prompt');
  fieldset.querySelectorAll('input[name="pricingGroup"]').forEach((input) => {
    input.checked = state.pricingGroup === input.value;
    input.closest('.group-card').classList.toggle('is-selected', input.checked);
  });
}

function renderProducts() {
  const host = $('productList');
  host.innerHTML = '';
  if (!state.products.length) {
    host.innerHTML = '<p class="empty-state">No tickets are on sale for this date.</p>';
    $('promoBlock').hidden = true;
    return;
  }
  if (!state.pricingGroup) {
    host.innerHTML = `<p class="hint choose-group-hint">${escapeHtml(t('choose_group'))}</p>`;
    $('promoBlock').hidden = true;
    return;
  }
  const product = productForGroup(state.pricingGroup);
  if (!product) {
    host.innerHTML = '<p class="empty-state">No tickets are on sale for this group.</p>';
    $('promoBlock').hidden = true;
    return;
  }
  const card = document.createElement('div');
  card.className = 'product';
  const heading = document.createElement('h3');
  heading.textContent = pick(product.name, product.code);
  card.appendChild(heading);
  const desc = pick(product.description, '');
  if (desc) {
    const p = document.createElement('p');
    p.textContent = desc;
    card.appendChild(p);
  }
  const grid = document.createElement('div');
  grid.className = 'tt-grid';
  product.ticket_types.forEach((tt) => grid.appendChild(ticketCard(product, tt)));
  card.appendChild(grid);
  host.appendChild(card);
  $('promoBlock').hidden = false;
}

function ticketCard(product, tt) {
  const seg = segmentOf(tt.code);
  const label = pick(tt.name, tt.code);
  const max = tt.max_quantity || product.max_per_booking || 10;

  const card = document.createElement('div');
  card.className = 'tt-card';
  card.dataset.seg = seg;

  const icon = document.createElement('span');
  icon.className = 'tt-icon';
  icon.innerHTML = SEG_ICONS[seg] || SEG_ICONS.DEFAULT;

  const info = document.createElement('div');
  info.className = 'tt-info';
  const name = document.createElement('strong');
  name.className = 'tt-cardname';
  name.textContent = label;
  info.appendChild(name);
  const descText = pick(tt.description, '');
  if (descText) {
    const d = document.createElement('small');
    d.className = 'tt-desc';
    d.textContent = descText;
    info.appendChild(d);
  }
  const price = document.createElement('span');
  price.className = 'tt-price';
  price.textContent = money(tt.unit_price_minor, tt.currency);
  info.appendChild(price);

  const qty = document.createElement('div');
  qty.className = 'qty';
  const minus = document.createElement('button');
  minus.type = 'button';
  minus.textContent = '\u2212';
  minus.setAttribute('aria-label', `Remove one ${label}`);
  const out = document.createElement('output');
  out.textContent = '0';
  out.setAttribute('aria-label', `${label} quantity`);
  const plus = document.createElement('button');
  plus.type = 'button';
  plus.textContent = '+';
  plus.setAttribute('aria-label', `Add one ${label}`);

  const sync = () => {
    const n = state.quantities[tt.id] || 0;
    out.textContent = String(n);
    minus.disabled = n === 0;
    plus.disabled = n >= max;
    card.classList.toggle('has-qty', n > 0);
  };
  minus.addEventListener('click', () => { adjust(tt.id, -1, max); sync(); });
  plus.addEventListener('click', () => {
    if ((state.quantities[tt.id] || 0) >= max) { toast(t('max_per_booking', { max }), 'error'); return; }
    adjust(tt.id, 1, max); sync();
  });
  sync();

  qty.append(minus, out, plus);
  card.append(icon, info, qty);
  return card;
}

function adjust(ticketTypeId, delta, max) {
  const next = Math.max(0, Math.min(max, (state.quantities[ticketTypeId] || 0) + delta));
  if (next === 0) delete state.quantities[ticketTypeId]; else state.quantities[ticketTypeId] = next;
  requestQuote();
}

// Switching Thai <-> International changes which product's ticket types apply, so any
// quantities from the other group are cleared (they belong to a different product).
function pickPricingGroup(group) {
  if (state.pricingGroup === group) return;
  state.pricingGroup = group;
  state.quantities = {};
  state.quote = null;
  clearHold();
  renderPricingGroup();
  renderProducts();
  renderSummary();
}

let quoteTimer = null;
function requestQuote() {
  clearTimeout(quoteTimer);
  quoteTimer = setTimeout(doQuote, 260);
}

async function doQuote() {
  const lines = Object.entries(state.quantities).map(([id, quantity]) => ({ ticket_type_id: id, quantity }));
  if (!lines.length) {
    // Nothing selected: fold the later panels away but stay on ticket selection.
    state.quote = null;
    clearHold();
    renderSummary();
    gotoStep(2);
    return;
  }
  try {
    state.quote = await api('/api/quote', {
      method: 'POST',
      body: JSON.stringify({
        visit_date: state.selectedDate,
        lines,
        promotion_codes: state.promoCodes,
      }),
    });
    startHold(state.quote.holds);
    renderSummary();
    // A valid quote reveals the details step and the "Make a Payment" CTA. It does
    // NOT auto-advance — the customer stays in control.
    gotoStep(3);
  } catch (e) {
    state.quote = null;
    renderSummary();
    toast(e.message, 'error');
    if (e.details && e.details.nearest_available_dates && e.details.nearest_available_dates.length) {
      $('dateHint').textContent = `Nearest available: ${e.details.nearest_available_dates.map(fmtDate).join(', ')}`;
    }
  }
}

function renderNextSteps(host) {
  // R11.9 — tell the customer what happens next before they pay: how the ticket is
  // delivered, when it is valid, where to enter, and the applicable policy.
  const steps = (state.quote && state.quote.next_steps) || [];
  if (!host) return;
  if (!steps.length) { host.innerHTML = ''; return; }
  host.innerHTML = `<h3>${escapeHtml(t('before_you_pay'))}</h3><ul>`
    + steps.map((s) => `<li>${escapeHtml(typeof s === 'string' ? s : (s.text || s.label || ''))}</li>`).join('')
    + '</ul>';
}

/* ------------------------------------------------------------------ summary */

function renderSummary() {
  const body = $('summaryBody');
  const count = $('sumCount');
  if (!state.quote) {
    body.innerHTML = state.selectedDate
      ? '<p class="muted">Add tickets to see your total.</p>'
      : '<p class="muted">Choose a date to begin.</p>';
    count.hidden = true;
    togglePayCta(false);
    return;
  }
  const s = state.quote.summary;
  // Ticket count badge beside the "Your order" heading.
  const totalQty = (s.lines || []).reduce((n, line) => n + line.quantity, 0);
  count.textContent = `${totalQty} ${totalQty === 1 ? 'ticket' : 'tickets'}`;
  count.hidden = totalQty === 0;
  const rows = s.lines.map((line) => `
    <div class="sum-line">
      <span class="q">${line.quantity} \u00d7 ${escapeHtml(ttNameByCode(line.ticket_type_code))}</span>
      <span>${money(line.gross_minor, s.currency)}</span>
    </div>`).join('');
  const promos = (s.applied_promotions || []).map((p) => `
    <div class="sum-line promo-line">
      <span class="q">${escapeHtml(p.name)}</span>
      <span>\u2212${money(p.amount_minor, s.currency)}</span>
    </div>`).join('');
  // Show the rounding adjustment whenever it is non-zero, so the lines the customer
  // reads actually add up to the amount charged (R5.5). Hiding it is how "the total
  // doesn't match" support tickets happen.
  const adj = state.quote.rounding_adjustment_minor || 0;
  const rounding = adj === 0 ? '' : `
    <div class="sum-line"><span class="q">Rounding</span><span>${adj < 0 ? '\u2212' : '+'}${money(Math.abs(adj), s.currency)}</span></div>`;
  const taxNote = state.venue && state.venue.tax_model === 'INCLUSIVE'
    ? 'VAT included' : 'VAT added at payment';
  // Date and venue as tiles, so the two facts a guest double-checks before paying
  // read at a glance instead of hiding in a list of rows.
  const venueName = state.venue ? pick(state.venue.name, state.venue.code) : '';
  const tiles = `
    <div class="sum-tiles">
      <div class="sum-tile">
        <p class="lbl">${escapeHtml(t('date_label'))}</p>
        <p class="val">${escapeHtml(fmtDate(s.visit_date))}</p>
      </div>
      <div class="sum-tile">
        <p class="lbl">${escapeHtml(t('venue_label'))}</p>
        <p class="val">${escapeHtml(venueName)}</p>
      </div>
    </div>`;
  body.innerHTML = `
    ${tiles}
    ${rows}${promos}${rounding}
    <div class="sum-total"><span>${escapeHtml(t('total'))}</span><span>${money(state.quote.total_minor, s.currency)}</span></div>
    <p class="tax-note">${taxNote}</p>`;
  togglePayCta(true);
}

// Show/hide both the aside CTA and the mobile sticky bar. The Make-a-Payment button
// is the primary emphasis (§16); the hold timer beneath it stays subtle.
function togglePayCta(show) {
  $('payCta').hidden = !show;
  const sticky = $('stickyPay');
  sticky.hidden = !show;
  if (show && state.quote) {
    const total = money(state.quote.total_minor, (state.quote.summary || {}).currency);
    $('stickyTotal').textContent = total;
  }
}

/* ------------------------------------------------------------------ hold */

// General admission at Aquaria is uncapped, so a quote can legitimately carry no
// holds — that is normal, not an error. The subtle timer only shows when a hold exists.
function startHold(holds) {
  clearHold();
  if (!holds || !holds.length) { $('holdSubtle').hidden = true; return; }
  const soonest = holds.reduce((a, b) => (a.remaining_seconds < b.remaining_seconds ? a : b));
  state.holdExpiresAt = Date.now() + soonest.remaining_seconds * 1000;
  $('holdSubtle').hidden = false;
  tickHold();
  state.holdTimer = setInterval(tickHold, 1000);
}

function tickHold() {
  const left = Math.max(0, Math.round((state.holdExpiresAt - Date.now()) / 1000));
  const hold = $('holdSubtle');
  const clock = `${String(Math.floor(left / 60)).padStart(2, '0')}:${String(left % 60).padStart(2, '0')}`;
  // Subtle by default; becomes a clear warning close to expiry (§17).
  $('holdSubtleText').textContent = t('seats_reserved', { time: clock });
  hold.classList.toggle('urgent', left <= 120);
  if (left <= 0) {
    clearHold();
    hold.hidden = true;
    // R10.7 — tell the customer explicitly and return them to selection with their
    // choices preserved; never silently drop the cart.
    toast(t('hold_expired'), 'error');
    doQuote();
  }
}

function clearHold() {
  if (state.holdTimer) clearInterval(state.holdTimer);
  state.holdTimer = null;
  $('holdSubtle').hidden = true;
}

/* ------------------------------------------------------------------ review popup */

// Payment-type icons (§20-§25). Clean inline marks by the API's `icon` field; we do
// not fabricate brand logos — Alipay/WeChat get a neutral wallet mark plus their name.
const PAY_ICONS = {
  qr:
    '<svg viewBox="0 0 24 24" class="pay-svg" aria-hidden="true" focusable="false">'
    + '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>'
    + '<rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM20 14v7M17 20h4M14 20v1"/></svg>',
  card:
    '<svg viewBox="0 0 24 24" class="pay-svg" aria-hidden="true" focusable="false">'
    + '<rect x="2" y="5" width="20" height="14" rx="2.5"/><path d="M2 9h20M6 15h5"/></svg>',
  wallet:
    '<svg viewBox="0 0 24 24" class="pay-svg" aria-hidden="true" focusable="false">'
    + '<rect x="3" y="6" width="18" height="13" rx="2.5"/><path d="M3 10h18M16 13.5h2.5"/></svg>',
};

function payIcon(iconField) {
  const key = String(iconField || '').toLowerCase();
  if (key === 'qr' || key === 'promptpay') return PAY_ICONS.qr;
  if (key === 'card' || key === 'credit_card') return PAY_ICONS.card;
  return PAY_ICONS.wallet; // alipay, wechat, e-wallet and anything else
}

// Build the review popup and open it. Opening does not touch the hold — holds are
// server-side and released only on abandon/expiry, so we simply never abandon here.
async function openReview() {
  const errors = validateDetails();
  if (errors) { toast(errors, 'error'); return; }
  $('rvErr').textContent = '';
  renderReview();
  renderNextSteps($('rvNextSteps'));
  await loadPaymentTypes();
  applyReviewText();
  $('reviewDialog').showModal();
  $('rvPay').focus();
}

function renderReview() {
  const s = state.quote.summary;
  const cur = s.currency;

  // Your visit.
  const sessionLine = state.quote.session_label
    ? `<div class="rv-line"><span>${escapeHtml(t('session'))}</span><span>${escapeHtml(state.quote.session_label)}</span></div>` : '';
  $('rvVisit').innerHTML =
    `<div class="rv-line"><span>${escapeHtml(pick(state.venue.name, state.venue.code))}</span><span>${escapeHtml(fmtDate(s.visit_date))}</span></div>${sessionLine}`;

  // Visitors & tickets: the pricing group plus per-segment quantities.
  const groupName = state.pricingGroup === 'LOCAL' ? t('group_local') : t('group_intl');
  const segTotals = { ADULT: 0, CHILD: 0, SENIOR: 0, DEFAULT: 0 };
  (s.lines || []).forEach((line) => { segTotals[segmentOf(line.ticket_type_code)] += line.quantity; });
  const segRows = (s.lines || []).map((line) =>
    `<div class="rv-line"><span>${line.quantity} \u00d7 ${escapeHtml(ttNameByCode(line.ticket_type_code))}</span><span>${money(line.gross_minor, cur)}</span></div>`).join('');
  $('rvVisitors').innerHTML = `<p class="rv-group">${escapeHtml(groupName)}</p>${segRows}`;

  // Promotions.
  const promos = s.applied_promotions || [];
  $('rvPromoSec').hidden = !promos.length;
  $('rvPromos').innerHTML = promos.map((p) =>
    `<div class="rv-line promo-line"><span>${escapeHtml(p.name)}</span><span>\u2212${money(p.amount_minor, cur)}</span></div>`).join('');

  // Price summary from the authoritative charge breakdown.
  $('rvPrice').innerHTML = priceSummaryHtml(state.quote.charges || {}, state.quote, cur);

  // Customer details.
  $('rvDetails').innerHTML =
    `<div class="rv-cust"><strong>${escapeHtml($('fullName').value.trim())}</strong>`
    + `<span>${escapeHtml($('email').value.trim())}</span>`
    + ($('phone').value.trim() ? `<span>${escapeHtml($('phone').value.trim())}</span>` : '')
    + '</div>';
}

function priceSummaryHtml(ch, quote, cur) {
  cur = cur || ch.currency || 'THB';
  const line = (label, value, cls) =>
    `<div class="rv-line${cls ? ' ' + cls : ''}"><span>${escapeHtml(label)}</span><span>${value}</span></div>`;
  const discount = (ch.line_discount_minor || 0) + (ch.order_discount_minor || 0);
  let html = line(t('subtotal'), money(ch.subtotal_minor != null ? ch.subtotal_minor : (quote.summary || {}).gross_minor, cur));
  if (discount > 0) html += line(t('discount'), '\u2212' + money(discount, cur), 'promo-line');
  if (ch.service_charge_minor) {
    const lbl = t('service_charge') + (ch.service_charge_included ? ' ' + t('included') : '');
    html += line(lbl, money(ch.service_charge_minor, cur));
  }
  if (ch.vat_minor) {
    const lbl = t('vat') + (ch.vat_included ? ' ' + t('included') : '');
    html += line(lbl, money(ch.vat_minor, cur));
  }
  const adj = quote.rounding_adjustment_minor || ch.rounding_adjustment_minor || 0;
  if (adj !== 0) html += line(t('rounding'), (adj < 0 ? '\u2212' : '+') + money(Math.abs(adj), cur));
  const grand = quote.total_minor != null ? quote.total_minor : ch.grand_total_minor;
  html += `<div class="rv-total"><span>${escapeHtml(t('total'))}</span><span>${money(grand, cur)}</span></div>`;
  return html;
}

async function loadPaymentTypes() {
  const host = $('rvPayTypes');
  let data;
  try {
    const cur = (state.quote.summary || {}).currency;
    data = await api('/api/payment-types' + (cur ? `?currency=${encodeURIComponent(cur)}` : ''));
  } catch (e) {
    host.innerHTML = `<p class="muted">${escapeHtml(t('no_payment'))}</p>`;
    state.paymentTypes = []; state.paymentTypeId = null; state.paymentMethod = null;
    return;
  }
  state.paymentTypes = data.payment_types || [];
  if (!state.paymentTypes.length) {
    host.innerHTML = `<p class="muted">${escapeHtml(t('no_payment'))}</p>`;
    state.paymentTypeId = null; state.paymentMethod = null;
    return;
  }
  // Keep a prior selection if still offered, otherwise default to the first.
  if (!state.paymentTypes.some((p) => p.id === state.paymentTypeId)) {
    state.paymentTypeId = state.paymentTypes[0].id;
    state.paymentMethod = state.paymentTypes[0].method;
  }
  host.innerHTML = '';
  state.paymentTypes.forEach((pt) => {
    const selected = pt.id === state.paymentTypeId;
    const label = document.createElement('label');
    label.className = 'pay-card' + (selected ? ' is-selected' : '');
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'payType';
    input.value = pt.id;
    input.checked = selected;
    input.addEventListener('change', () => selectPaymentType(pt.id));
    const icon = document.createElement('span');
    icon.className = 'pay-icon';
    icon.innerHTML = payIcon(pt.icon);
    const body = document.createElement('span');
    body.className = 'pay-text';
    const nm = document.createElement('strong');
    nm.textContent = pt.display_name || pt.code;
    body.appendChild(nm);
    if (pt.description) {
      const d = document.createElement('small');
      d.textContent = pt.description;
      body.appendChild(d);
    }
    const check = document.createElement('span');
    check.className = 'pay-check';
    check.setAttribute('aria-hidden', 'true');
    label.append(input, icon, body, check);
    host.appendChild(label);
  });
}

function selectPaymentType(id) {
  const pt = state.paymentTypes.find((p) => p.id === id);
  if (!pt) return;
  state.paymentTypeId = pt.id;
  state.paymentMethod = pt.method;
  $('rvPayTypes').querySelectorAll('.pay-card').forEach((card) => {
    const input = card.querySelector('input');
    const on = input.value === id;
    card.classList.toggle('is-selected', on);
    input.checked = on;
  });
  // Announce the selection for assistive technology (spec §accessibility).
  toast(t('payment_selected', { name: pt.display_name || pt.code }), 'info');
}

// Localize the review dialog's static labels (data-t) and the Pay CTA amount.
function applyReviewText() {
  applyStaticText();
  const cur = (state.quote && state.quote.summary || {}).currency;
  const grand = state.quote ? state.quote.total_minor : 0;
  $('rvPay').textContent = t('pay', { amount: money(grand, cur) });
}

/* ------------------------------------------------------------------ consent + pay */

async function openConsent() {
  const errors = validateDetails();
  if (errors) { toast(errors, 'error'); return; }
  try {
    state.consent = await api('/api/consent');
  } catch (e) { toast(e.message, 'error'); return; }

  const c = state.consent;
  $('consentIntro').textContent =
    `${c.controller.name} is the data controller. We need your agreement before we submit your details.`;
  $('consentItems').innerHTML = c.items.map((item, i) => `
    <label class="consent-item${item.required ? ' required' : ''}">
      <input type="checkbox" id="ci-${i}" data-code="${item.code}" data-required="${item.required}">
      <span class="txt">
        <strong>${item.required ? t('required') : t('optional_label')} \u00b7 ${escapeHtml(item.code.replace(/_/g, ' ').toLowerCase())}${item.required ? '<span class="req-star" aria-hidden="true"> *</span>' : ''}</strong>
        ${escapeHtml(item.label)}
        <span class="basis">${escapeHtml(t('lawful_basis'))}: ${escapeHtml(item.lawful_basis)}</span>
      </span>
    </label>`).join('');
  $('consentDetail').innerHTML = `
    <dl>
      <dt>Who holds your data</dt><dd>${escapeHtml(c.controller.name)} \u00b7 ${escapeHtml(c.controller.contact)}</dd>
      <dt>Data protection officer</dt><dd>${escapeHtml(c.dpo_contact)}</dd>
      <dt>Retention</dt><dd>${escapeHtml(JSON.stringify(c.retention))}</dd>
      <dt>Recipients</dt><dd>${c.recipients.map((r) => escapeHtml(r.name)).join(', ')}</dd>
      <dt>Transfers outside Thailand</dt><dd>${c.cross_border.transfers
        ? escapeHtml(`Yes \u2014 ${(c.cross_border.countries || []).join(', ')} under ${c.cross_border.safeguard}`)
        : 'No'}</dd>
      <dt>Your rights</dt><dd>${c.rights.join(', ')}</dd>
      <dt>Privacy notice</dt><dd>${escapeHtml(c.notice_url)} (version ${escapeHtml(c.notice_version)})</dd>
    </dl>`;
  $('consentDisabledReason').textContent = c.submit_disabled_reason;
  $('consentErr').textContent = '';
  $('consentAccept').disabled = true;

  // Every box starts unchecked and the submit stays disabled until the required
  // item is affirmatively accepted (R12.5, R12.7).
  $('consentItems').querySelectorAll('input[type=checkbox]').forEach((box) => {
    box.addEventListener('change', () => {
      const required = Array.from($('consentItems').querySelectorAll('input[data-required="true"]'));
      $('consentAccept').disabled = !required.every((r) => r.checked);
    });
  });
  $('consentDialog').showModal();
}

function collectConsent() {
  const items = {};
  $('consentItems').querySelectorAll('input[type=checkbox]').forEach((box) => {
    items[box.dataset.code] = box.checked;
  });
  return items;
}

function validateDetails() {
  let message = null;
  const email = $('email');
  const name = $('fullName');
  $('err-email').textContent = '';
  $('err-full_name').textContent = '';
  email.setAttribute('aria-invalid', 'false');
  name.setAttribute('aria-invalid', 'false');
  if (!name.value.trim()) {
    $('err-full_name').textContent = t('enter_name');
    name.setAttribute('aria-invalid', 'true');
    message = t('complete_details');
  }
  if (!email.value.includes('@')) {
    $('err-email').textContent = t('enter_email');
    email.setAttribute('aria-invalid', 'true');
    message = t('complete_details');
  }
  if (!state.quote) message = t('choose_tickets_first');
  return message;
}

async function pay() {
  const consentItems = collectConsent();
  const method = state.paymentMethod;
  if (!method) { $('consentErr').textContent = 'Choose a payment method first.'; return; }
  const button = $('consentAccept');
  button.disabled = true;
  button.textContent = t('processing');
  try {
    const result = await api('/api/confirm', {
      method: 'POST',
      body: JSON.stringify({
        email: $('email').value.trim(),
        full_name: $('fullName').value.trim(),
        phone: $('phone').value.trim() || null,
        consent_items: consentItems,
        payment_method: method,
      }),
    });
    $('consentDialog').close();
    const reviewDlg = $('reviewDialog');
    if (reviewDlg.open) reviewDlg.close();
    if (result.confirmed) {
      renderConfirmation(result);
    } else {
      // R10.8 — money taken, inventory gone. Say so plainly.
      toast(result.message || t('could_not_book'), 'error');
      $('err-confirm').textContent = result.message || '';
    }
  } catch (e) {
    // Prefer the server's specific, actionable text: a per-field message when present,
    // otherwise the error message. Never show only the generic "check the highlighted
    // fields" line when we have something more useful (R66.3).
    const fields = (e.details && e.details.fields) || {};
    const fieldMsg = Object.values(fields)[0];
    const shown = fieldMsg || e.message;
    $('consentErr').textContent = shown + (e.reference ? ` (ref ${e.reference})` : '');
    toast(shown, 'error');
    // If the saved selection expired (e.g. a prior attempt already consumed it, or the
    // session refreshed), retrying payment cannot succeed — send the guest back to
    // choose their tickets again rather than looping on a dead quote.
    if (fields.quote || e.code === 'hold_expired') {
      $('consentDialog').close();
      const reviewDlg = $('reviewDialog');
      if (reviewDlg.open) reviewDlg.close();
      clearHold();
      state.quote = null;
      gotoStep(2);
      renderSummary();
    }
  } finally {
    button.textContent = t('accept_and_pay');
    button.disabled = false;
  }
}

function renderConfirmation(result) {
  state.lastBooking = result;
  clearHold();
  togglePayCta(false);
  gotoStep(4);
  $('doneSummary').textContent = t('done_summary', {
    number: result.booking_number,
    count: result.tickets.length,
    amount: money(result.total_minor, result.currency),
    email: $('email').value.trim(),
  });
  $('ticketList').innerHTML = result.tickets.map((tk) => ticketCardHtml(tk)).join('');
  wireTicketPrintButtons($('ticketList'));
  window.scrollTo({ top: 0, behavior: 'smooth' });
  toast(t('booking_done_toast'), 'info');
}

// A ticket's stored valid_until is a UTC instant with the venue's timezone
// snapshotted beside it. Render it in that timezone — printing the raw UTC value
// tells a guest their ticket expires seven hours before it really does.
function ticketValidUntil(tk) {
  const iso = tk.valid_until;
  if (!iso) return '';
  const tz = tk.validity_timezone || (state.venue && state.venue.timezone) || undefined;
  try {
    return new Date(iso).toLocaleString(localeFor(), {
      timeZone: tz, dateStyle: 'medium', timeStyle: 'short',
    });
  } catch (_) {
    return iso.replace('T', ' ').replace('Z', ' UTC');
  }
}

// One ticket on the confirmation screen: a real scannable QR served from this
// origin, the key facts, and the two print routes.
function ticketCardHtml(tk) {
  const id = encodeURIComponent(tk.id);
  const entries = tk.unlimited_entries ? escapeHtml(t('tk_unlimited')) : tk.entry_allowance;
  return `
    <div class="ticket">
      <img class="qr-img" src="/tickets/${id}/qr.svg" width="132" height="132"
           alt="${escapeHtml(t('tk_qr_alt', { number: tk.ticket_number }))}">
      <dl>
        <dt>${escapeHtml(t('tk_ticket'))}</dt><dd>${escapeHtml(tk.ticket_number)}</dd>
        <dt>${escapeHtml(t('tk_visit'))}</dt><dd>${escapeHtml(tk.visit_date || '')}</dd>
        <dt>${escapeHtml(t('tk_valid'))}</dt><dd>${escapeHtml(ticketValidUntil(tk))}</dd>
        <dt>${escapeHtml(t('tk_entries'))}</dt><dd>${tk.entries_used} / ${entries}</dd>
      </dl>
      <div class="ticket-actions">
        <button type="button" class="ghost" data-print="eticket" data-ticket="${escapeHtml(tk.id)}">
          ${escapeHtml(t('print_eticket'))}
        </button>
        <button type="button" class="ghost" data-print="thermal" data-ticket="${escapeHtml(tk.id)}">
          ${escapeHtml(t('print_thermal'))}
        </button>
      </div>
    </div>`;
}

// Delegated so it works for both the confirmation screen and Manage Booking, and
// so no inline handler is needed (the CSP forbids them).
function wireTicketPrintButtons(root) {
  if (!root || root._printWired) return;
  root._printWired = true;
  root.addEventListener('click', (event) => {
    const button = event.target.closest('[data-print]');
    if (!button) return;
    openTicketForPrint(button.dataset.ticket, button.dataset.print);
  });
}

// The document prints itself (see web/print.js) rather than this window calling
// print() on it: waiting for another document's load event across a window
// handle is unreliable, and a dialog raised before the QR has painted can print
// a blank code.
function openTicketForPrint(ticketId, kind) {
  if (!ticketId) return;
  const url = `/tickets/${encodeURIComponent(ticketId)}/${kind}?print=1`;
  const opened = window.open(url, '_blank');
  if (!opened) toast(t('print_blocked'), 'error');
}

/* ------------------------------------------------------------------ shows */

async function loadShows(date) {
  state.showDate = date;
  let data;
  try {
    data = await api(`/api/shows?date=${date}&filter=${state.showFilter}`);
  } catch (e) { toast(e.message, 'error'); return; }

  $('showTz').textContent = data.timezone_label;
  $('showDates').innerHTML = '';
  data.quick_dates.forEach((d) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'date-chip' + (d.is_past ? ' past' : '');
    chip.setAttribute('aria-pressed', String(d.is_selected));
    chip.innerHTML = `<strong>${escapeHtml(d.label)}</strong><span>${d.date.slice(5)}</span>`;
    chip.addEventListener('click', () => loadShows(d.date));
    $('showDates').appendChild(chip);
  });

  $('showFilters').innerHTML = '';
  data.filters.forEach((f) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    chip.textContent = f.label;
    chip.setAttribute('aria-pressed', String(f.key === state.showFilter));
    chip.addEventListener('click', () => { state.showFilter = f.key; loadShows(state.showDate); });
    $('showFilters').appendChild(chip);
  });

  const list = $('showList');
  if (!data.sessions.length) {
    list.innerHTML = `<div class="empty-state"><p>${escapeHtml(data.empty_reason || t('nothing_scheduled'))}</p>${
      data.suggested_date ? `<p>${escapeHtml(t('next_shows'))}: ${fmtDate(data.suggested_date)}</p>` : ''}</div>`;
    return;
  }
  list.innerHTML = data.sessions.map((s) => `
    <article class="show${s.live_state === 'FINISHED' ? ' finished' : ''}" aria-label="${escapeHtml(s.accessible_label)}">
      <div class="time">${escapeHtml(s.start_time)}<small>${t('show_min', { n: s.duration_minutes })}</small></div>
      <div>
        <h3>${escapeHtml(s.show_name)}</h3>
        <p class="where">${escapeHtml(s.location_display_name || '')}${
          s.location_differs_from_usual ? ' \u00b7 ' + escapeHtml(t('diff_location')) : ''}</p>
        <span class="badge" data-s="${s.live_state}">${escapeHtml(s.presentation.label)}</span>
        ${s.reservation_required ? `<span class="badge" data-s="UPCOMING">${escapeHtml(t('show_reservation_required'))}</span>`
          : `<span class="badge" data-s="FINISHED">${escapeHtml(t('show_included'))}</span>`}
        ${s.show_countdown ? `<span class="badge" data-s="STARTING_SOON">${escapeHtml(t('show_in_min', { n: s.minutes_to_start }))}</span>` : ''}
      </div>
    </article>`).join('');
}

/* ------------------------------------------------------------------ manage */

async function requestCode() {
  $('mHint').textContent = '';
  try {
    const result = await api('/api/manage/request-code', {
      method: 'POST',
      body: JSON.stringify({ booking_number: $('mBooking').value.trim(), email: $('mEmail').value.trim() }),
    });
    $('mCodeBlock').hidden = false;
    $('mHint').textContent = result.message;
    if (result.demo_code) {
      $('mCode').value = result.demo_code;
      $('mHint').textContent += ` (demo: code pre-filled)`;
    }
  } catch (e) { toast(e.message, 'error'); }
}

async function verifyCode() {
  try {
    const view = await api('/api/manage/verify', {
      method: 'POST',
      body: JSON.stringify({
        booking_number: $('mBooking').value.trim(),
        email: $('mEmail').value.trim(),
        code: $('mCode').value.trim(),
      }),
    });
    const policy = view.policy || {};
    $('mResult').innerHTML = `
      <div class="panel" style="margin-top:16px">
        <h3>${escapeHtml(view.booking_number)} \u00b7 ${escapeHtml(view.status)}</h3>
        <p>${escapeHtml(view.venue)} \u00b7 ${escapeHtml(view.visit_date || '')} \u00b7 ${money(view.total_minor, view.currency)}</p>
        <p class="hint">Reschedule: ${policy.reschedule && policy.reschedule.allowed ? 'available' : escapeHtml((policy.reschedule || {}).reason || 'not available')}
          \u00b7 Cancel: ${policy.cancel && policy.cancel.allowed
            ? `refundable ${money((policy.cancel || {}).refundable_minor, view.currency)}`
            : escapeHtml((policy.cancel || {}).reason || 'not available')}</p>
        <div class="tickets">${view.tickets.map((tk) => `
          <div class="ticket">
            <img class="qr-img" src="/tickets/${encodeURIComponent(tk.ticket_id)}/qr.svg"
                 width="132" height="132"
                 alt="${escapeHtml(t('tk_qr_alt', { number: tk.ticket_number }))}">
            <dl><dt>${escapeHtml(t('tk_ticket'))}</dt><dd>${escapeHtml(tk.ticket_number)}</dd>
            <dt>${escapeHtml(t('tk_type'))}</dt><dd>${escapeHtml(tk.ticket_type)} (${escapeHtml(tk.segment)})</dd>
            <dt>${escapeHtml(t('tk_state'))}</dt><dd>${escapeHtml(tk.state)}</dd>
            <dt>${escapeHtml(t('tk_valid'))}</dt><dd>${escapeHtml(ticketValidUntil({
              valid_until: tk.valid_until, validity_timezone: tk.venue_timezone,
            }))}</dd></dl>
            <div class="ticket-actions">
              <button type="button" class="ghost" data-print="eticket" data-ticket="${escapeHtml(tk.ticket_id)}">
                ${escapeHtml(t('print_eticket'))}
              </button>
              <button type="button" class="ghost" data-print="thermal" data-ticket="${escapeHtml(tk.ticket_id)}">
                ${escapeHtml(t('print_thermal'))}
              </button>
            </div>
          </div>`).join('')}</div>
      </div>`;
    wireTicketPrintButtons($('mResult'));
  } catch (e) { toast(e.message, 'error'); }
}

/* ------------------------------------------------------------------ ops */

async function staffLogin() {
  try {
    const result = await api('/api/staff/login', {
      method: 'POST',
      body: JSON.stringify({ email: $('sEmail').value, credential: $('sPass').value, mfa_code: '000000' }),
    });
    // Through utpAuth so the token is persisted and the back office sees the same
    // session. Two modules each holding their own copy is how one keeps using a
    // token the other has already revoked.
    utpAuth.set(result.token);
    state.settings = null;
    $('settingsPanel').hidden = true;
    $('sHint').textContent = `Signed in as ${result.display_name} \u00b7 roles: ${result.roles.join(', ')} \u00b7 authority ${result.authority_level}`;
    const nav = await api('/api/staff/navigation');
    $('sNav').innerHTML = nav.navigation.length
      ? nav.navigation.map((n) => `<li><span>${escapeHtml(n.label)}</span><span class="verbs">${
          ['VIEW', n.can_add && 'ADD', n.can_edit && 'EDIT', n.can_delete && 'DELETE'].filter(Boolean).join(' ')
        }</span></li>`).join('')
      : '<li class="muted">This role can view no pages.</li>';
  } catch (e) {
    $('sHint').textContent = e.message;
    toast(e.message, 'error');
  }
}

async function staffCall(path, label) {
  if (!state.staffToken) { toast('Sign in first.', 'error'); return; }
  try {
    const data = await api(path);
    $('sOut').textContent = `${label}\n\n` + JSON.stringify(data, null, 2);
  } catch (e) {
    // Demonstrates R42: the API refuses regardless of what the UI showed.
    $('sOut').textContent = `${label}\n\nREFUSED: ${e.message}\ncode: ${e.code}`;
    toast(e.message, 'error');
  }
}

/* ------------------------------------------------------------------ settings */

function todayIsoLocal() { return todayIso(); }

// Build one field row. Returns the wrapper element; the input carries data-key.
function settingField(label, input, help) {
  const wrap = document.createElement('label');
  wrap.className = 'set-field';
  const span = document.createElement('span');
  span.className = 'set-label';
  span.textContent = label;
  wrap.append(span, input);
  if (help) {
    const small = document.createElement('small');
    small.className = 'hint';
    small.textContent = help;
    wrap.append(small);
  }
  return wrap;
}

function selectEl(options, value) {
  const sel = document.createElement('select');
  options.forEach(([v, text]) => {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = text;
    if (v === value) opt.selected = true;
    sel.append(opt);
  });
  return sel;
}

function reasonInput() {
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = 'Reason (recorded in the audit log)';
  input.className = 'set-reason';
  return input;
}

function settingsCard(title, editable) {
  const card = document.createElement('div');
  card.className = 'set-card';
  const h = document.createElement('h4');
  h.textContent = title;
  card.append(h);
  if (!editable) {
    const ro = document.createElement('span');
    ro.className = 'set-badge';
    ro.textContent = 'View only';
    h.append(ro);
  }
  return card;
}

async function loadSettings() {
  if (!state.staffToken) { toast('Sign in first.', 'error'); return; }
  let data;
  try {
    data = await api('/api/staff/settings');
  } catch (e) { toast(e.message, 'error'); return; }
  state.settings = data;
  $('settingsPanel').hidden = false;
  const body = $('settingsBody');
  body.innerHTML = '';

  // Group per spec §25: Business, Tax & Charges, Ticket & Access, Currency.
  const business = settingsGroup('Business');
  if (data.timezone) business.append(timezoneCard(data.timezone));
  if (data.base_currency) business.append(baseCurrencyCard(data.base_currency));
  if (business.childElementCount > 1) body.append(business);

  const tax = settingsGroup('Tax & charges');
  if (data.vat) tax.append(chargeCard('VAT', data.vat, '/api/staff/settings/vat', 'INCLUSIVE'));
  if (data.service_charge) {
    tax.append(chargeCard('Service charge', data.service_charge, '/api/staff/settings/service-charge', 'EXCLUSIVE'));
  }
  if (tax.childElementCount > 1) body.append(tax);

  const ticket = settingsGroup('Ticket & access');
  if (data.ticket_validity) ticket.append(validityCard(data.ticket_validity));
  if (ticket.childElementCount > 1) body.append(ticket);

  const currency = settingsGroup('Currency');
  if (data.exchange_rates) currency.append(exchangeRatesCard(data.exchange_rates, data.currency));
  if (currency.childElementCount > 1) body.append(currency);

  if (!body.childElementCount) {
    body.innerHTML = '<p class="muted">This role cannot view any settings.</p>';
  }
}

function settingsGroup(title) {
  const group = document.createElement('section');
  group.className = 'set-group';
  const h = document.createElement('h3');
  h.textContent = title;
  group.append(h);
  return group;
}

function saveRow(button, onSave) {
  button.addEventListener('click', async () => {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = 'Saving\u2026';
    try {
      await onSave();
      toast('Saved. The change is audited.', 'info');
      await loadSettings();
    } catch (e) {
      toast(e.message, 'error');
      button.disabled = false;
      button.textContent = original;
    }
  });
}

function chargeCard(title, block, path, defaultMode) {
  const cur = block.current || {};
  const card = settingsCard(title, block.can_edit);
  const pct = ((cur.rate_bp || 0) / 100).toFixed(2);
  const summary = document.createElement('p');
  summary.className = 'set-summary';
  summary.textContent = `${cur.enabled ? 'Enabled' : 'Disabled'} \u00b7 ${pct}% \u00b7 `
    + `${cur.mode === 'INCLUSIVE' ? 'Included in price' : 'Added at checkout'}`
    + (cur.effective_from ? ` \u00b7 from ${cur.effective_from}` : ' (from venue default)');
  card.append(summary);
  if (!block.can_edit) return card;

  const enabled = selectEl([['true', 'Enabled'], ['false', 'Disabled']], String(!!cur.enabled));
  const rate = document.createElement('input');
  rate.type = 'number'; rate.step = '0.01'; rate.min = '0'; rate.max = '100'; rate.value = pct;
  const mode = selectEl([['INCLUSIVE', 'Included in price'], ['EXCLUSIVE', 'Added at checkout']],
    cur.mode || defaultMode);
  const eff = document.createElement('input');
  eff.type = 'date'; eff.value = cur.effective_from || todayIsoLocal();
  const reason = reasonInput();
  card.append(
    settingField('Status', enabled),
    settingField('Rate (%)', rate),
    settingField('Mode', mode, 'Included: the price already contains this charge. Added: it is added at checkout.'),
    settingField('Effective from', eff, 'Earlier transactions keep the rate they were charged.'),
    settingField('Reason', reason),
  );
  const save = document.createElement('button');
  save.type = 'button'; save.className = 'primary'; save.textContent = 'Save ' + title;
  saveRow(save, () => api(path, {
    method: 'POST',
    body: JSON.stringify({
      enabled: enabled.value === 'true',
      rate_bp: Math.round(parseFloat(rate.value || '0') * 100),
      mode: mode.value,
      effective_from: eff.value,
      reason: reason.value.trim() || null,
    }),
  }));
  card.append(save);
  return card;
}

function timezoneCard(block) {
  const card = settingsCard('Time zone', block.can_edit);
  const summary = document.createElement('p');
  summary.className = 'set-summary';
  summary.textContent = block.timezone;
  card.append(summary);
  if (!block.can_edit) return card;
  const tz = selectEl([
    ['Asia/Bangkok', 'Asia/Bangkok (UTC+07:00)'],
    ['Asia/Tokyo', 'Asia/Tokyo (UTC+09:00)'],
    ['Asia/Singapore', 'Asia/Singapore (UTC+08:00)'],
    ['Europe/London', 'Europe/London'],
    ['America/New_York', 'America/New_York'],
  ], block.timezone);
  const reason = reasonInput();
  card.append(
    settingField('IANA time zone', tz, 'Tickets expire by the venue\u2019s local time. A UTC offset alone is rejected.'),
    settingField('Reason', reason),
  );
  const save = document.createElement('button');
  save.type = 'button'; save.className = 'primary'; save.textContent = 'Save time zone';
  saveRow(save, () => api('/api/staff/settings/timezone', {
    method: 'POST',
    body: JSON.stringify({ timezone: tz.value, reason: reason.value.trim() || null }),
  }));
  card.append(save);
  return card;
}

function baseCurrencyCard(block) {
  const card = settingsCard('Base currency', block.can_edit);
  const info = block.info || {};
  const summary = document.createElement('p');
  summary.className = 'set-summary';
  summary.textContent = `${block.currency} \u00b7 ${info.symbol || ''} \u00b7 ${info.decimals} decimal place(s)`;
  card.append(summary);
  if (!block.can_edit) return card;
  const cur = document.createElement('input');
  cur.type = 'text'; cur.maxLength = 3; cur.value = block.currency; cur.className = 'set-currency';
  const reason = reasonInput();
  card.append(
    settingField('ISO 4217 code', cur, 'For example THB, USD, JPY.'),
    settingField('Reason', reason),
  );
  const save = document.createElement('button');
  save.type = 'button'; save.className = 'primary'; save.textContent = 'Save currency';
  saveRow(save, () => api('/api/staff/settings/base-currency', {
    method: 'POST',
    body: JSON.stringify({ currency: cur.value.trim().toUpperCase(), reason: reason.value.trim() || null }),
  }));
  card.append(save);
  return card;
}

function validityCard(block) {
  const policy = block.policy || {};
  const card = settingsCard('Ticket validity', block.can_edit);
  const summary = document.createElement('p');
  summary.className = 'set-summary';
  summary.textContent = `${policy.validity_type} \u00b7 `
    + `${policy.reentry_allowed ? 're-entry allowed' : 'single entry'} \u00b7 max ${policy.max_entries} entr${policy.max_entries === 1 ? 'y' : 'ies'}`;
  card.append(summary);
  if (!block.can_edit) return card;
  const type = selectEl((block.validity_types || ['END_OF_VISIT_DAY']).map((t) => [t, t.replace(/_/g, ' ')]),
    policy.validity_type);
  const days = document.createElement('input');
  days.type = 'number'; days.min = '1'; days.value = policy.number_of_days || 1;
  const reentry = selectEl([['false', 'No'], ['true', 'Yes']], String(!!policy.reentry_allowed));
  const maxEntries = document.createElement('input');
  maxEntries.type = 'number'; maxEntries.min = '1'; maxEntries.value = policy.max_entries || 1;
  const reason = reasonInput();
  card.append(
    settingField('Validity type', type, 'Default is End of Visit Day: valid until 23:59:59 venue-local.'),
    settingField('Number of days', days, 'Used when validity type is Number of Days.'),
    settingField('Re-entry', reentry),
    settingField('Maximum entries', maxEntries),
    settingField('Reason', reason),
  );
  const save = document.createElement('button');
  save.type = 'button'; save.className = 'primary'; save.textContent = 'Save validity';
  saveRow(save, () => api('/api/staff/settings/ticket-validity', {
    method: 'POST',
    body: JSON.stringify({
      policy: {
        validity_type: type.value,
        number_of_days: parseInt(days.value || '1', 10),
        reentry_allowed: reentry.value === 'true',
        max_entries: parseInt(maxEntries.value || '1', 10),
      },
      reason: reason.value.trim() || null,
    }),
  }));
  card.append(save);
  return card;
}

function exchangeRatesCard(block, baseCurrency) {
  const card = settingsCard('Exchange rates', block.can_add || block.can_edit);
  const list = document.createElement('div');
  list.className = 'fx-list';
  const rates = block.rates || [];
  if (!rates.length) {
    list.innerHTML = '<p class="muted">No exchange rates configured.</p>';
  } else {
    rates.forEach((r) => {
      const row = document.createElement('div');
      row.className = 'fx-row' + (r.status === 'ACTIVE' ? '' : ' ended');
      const label = document.createElement('span');
      // Direction is always spelled out: "1 USD = 33.10 THB" (§21).
      label.textContent = `${r.direction} \u00b7 from ${r.effective_from} \u00b7 ${r.status}`;
      row.append(label);
      if (block.can_edit && r.status === 'ACTIVE') {
        const end = document.createElement('button');
        end.type = 'button'; end.className = 'ghost'; end.textContent = 'End';
        saveRow(end, () => api(`/api/staff/settings/exchange-rates/${encodeURIComponent(r.id)}/end`, {
          method: 'POST',
          body: JSON.stringify({ reason: 'Ended from back office' }),
        }));
        row.append(end);
      }
      list.append(row);
    });
  }
  card.append(list);
  if (!block.can_add) return card;

  const from = document.createElement('input');
  from.type = 'text'; from.maxLength = 3; from.placeholder = 'USD'; from.className = 'set-currency';
  const to = document.createElement('input');
  to.type = 'text'; to.maxLength = 3; to.value = baseCurrency || 'THB'; to.className = 'set-currency';
  const rate = document.createElement('input');
  rate.type = 'text'; rate.inputMode = 'decimal'; rate.placeholder = '33.10';
  const eff = document.createElement('input');
  eff.type = 'date'; eff.value = todayIsoLocal();
  const reason = reasonInput();
  const adder = document.createElement('div');
  adder.className = 'fx-add';
  const dirNote = document.createElement('p');
  dirNote.className = 'hint';
  const syncNote = () => {
    dirNote.textContent = `1 ${(from.value || 'USD').toUpperCase()} = ${rate.value || '\u2026'} ${(to.value || 'THB').toUpperCase()}`;
  };
  [from, to, rate].forEach((el) => el.addEventListener('input', syncNote));
  syncNote();
  adder.append(
    settingField('From (1 unit)', from),
    settingField('To', to),
    settingField('Rate', rate),
    settingField('Effective from', eff),
    settingField('Reason', reason),
  );
  card.append(adder, dirNote);
  const add = document.createElement('button');
  add.type = 'button'; add.className = 'primary'; add.textContent = 'Add exchange rate';
  saveRow(add, () => api('/api/staff/settings/exchange-rates', {
    method: 'POST',
    body: JSON.stringify({
      from_currency: from.value.trim().toUpperCase(),
      to_currency: to.value.trim().toUpperCase(),
      rate: rate.value.trim(),
      effective_from: eff.value,
      reason: reason.value.trim() || null,
    }),
  }));
  card.append(add);
  return card;
}

/* ------------------------------------------------------------------ gate */

async function gateScan() {
  const payload = $('gatePayload').value.trim();
  const box = $('gateResult');
  if (!payload) { toast('Paste a QR payload to scan.', 'error'); return; }
  let result;
  try {
    result = await api('/api/gate/scan', { method: 'POST', body: JSON.stringify({ qr_payload: payload }) });
  } catch (e) { toast(e.message, 'error'); return; }
  box.hidden = false;
  box.dataset.admit = String(!!result.admit);
  const prev = result.previous_admission
    ? `<p class="gate-prev">Previously admitted ${escapeHtml(result.previous_admission.at_local || result.previous_admission.at_utc || '')}`
      + `${result.previous_admission.access_point ? ' at ' + escapeHtml(result.previous_admission.access_point) : ''}.</p>`
    : '';
  const tkt = result.ticket
    ? `<p class="gate-tkt">Ticket ${escapeHtml(result.ticket.ticket_number)}${
        result.ticket.visit_date ? ' · ' + escapeHtml(result.ticket.visit_date) : ''}</p>`
    : '';
  // Two independent cues, never colour alone (R32.2, R68.4): a large word plus an icon.
  box.innerHTML = `
    <div class="gate-decision" data-admit="${!!result.admit}">
      <span class="gate-icon" aria-hidden="true">${result.admit ? '\u2713' : '\u2717'}</span>
      <strong>${escapeHtml(result.message || result.decision)}</strong>
    </div>${tkt}${prev}`;
}

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ------------------------------------------------------------------ language */

// Apply every translatable client-side label carrying data-t. Server-localized text
// (calendar, shows, consent, payment types) is refreshed by re-fetching with ?lang=.
function applyStaticText() {
  document.querySelectorAll('[data-t]').forEach((el) => {
    const key = el.getAttribute('data-t');
    if (key === 'pay') { return; }        // filled with the amount separately
    el.textContent = t(key);
  });
  // An icon-only control needs its accessible name translated too, and a title is
  // where a mouse user reads it. Kept separate from data-t because those elements
  // must not have their text content replaced.
  document.querySelectorAll('[data-t-title]').forEach((el) => {
    el.title = t(el.getAttribute('data-t-title'));
  });
  // The back office keeps its own string table (its screens are its own), so it is
  // asked to re-render rather than translated from here.
  if (window.utpBackoffice) window.utpBackoffice.relabel();
}

async function initLanguage() {
  let stored = 'en';
  try { stored = localStorage.getItem(LANG_STORAGE_KEY) || 'en'; } catch (_) { stored = 'en'; }
  if (SUPPORTED_LANGS.indexOf(stored) === -1) stored = 'en';
  state.lang = stored;
  document.documentElement.lang = stored;

  const select = $('langSelect');
  select.innerHTML = '';
  try {
    const data = await api('/api/languages');
    (data.languages || []).forEach((l) => {
      const opt = document.createElement('option');
      opt.value = l.code;
      // Name together with the indicator — never a flag alone (§1).
      opt.textContent = `${l.indicator ? l.indicator + ' ' : ''}${l.name}`;
      if (l.code === state.lang) opt.selected = true;
      select.appendChild(opt);
    });
  } catch (_) {
    // Fall back to the built-in list if the endpoint is unreachable.
    SUPPORTED_LANGS.forEach((code) => {
      const opt = document.createElement('option');
      opt.value = code; opt.textContent = code.toUpperCase();
      if (code === state.lang) opt.selected = true;
      select.appendChild(opt);
    });
  }
  applyStaticText();
}

async function setLanguage(code) {
  if (SUPPORTED_LANGS.indexOf(code) === -1) code = 'en';
  state.lang = code;
  document.documentElement.lang = code;
  try { localStorage.setItem(LANG_STORAGE_KEY, code); } catch (_) { /* ignore */ }
  applyStaticText();
  // The hero headline and lead are client-side strings, so they follow the switch too.
  if (state.venue) renderVenueChrome(pick(state.venue.name, state.venue.code));

  // Re-fetch server-localized content that is currently on screen so its text follows
  // the new language. Everything else picks up the language on its next request.
  if (calMonth) loadCalendar(calMonth);
  if (state.selectedDate) {
    await loadProducts(state.selectedDate);
    if (state.quote) doQuote();
  }
  renderSummary();
  if (state.showDate && !$('view-shows').hidden) loadShows(state.showDate);
  if ($('reviewDialog').open) { renderReview(); await loadPaymentTypes(); applyReviewText(); }
}

/* ------------------------------------------------------------------ boot */

function wire() {
  // Navigation goes through the router, not straight to showView, so that a
  // protected destination is checked before it is drawn and so the address bar
  // always reflects where the user is. Hiding a nav button is not the control
  // (§46) — the route guard and the API are.
  document.querySelectorAll('.nav-btn').forEach((b) =>
    b.addEventListener('click', () => {
      const route = b.dataset.route || `/${b.dataset.view}`;
      if (window.utpBackoffice) window.utpBackoffice.go(route);
      else showView(b.dataset.view);
    }));

  $('calPrev').addEventListener('click', () =>
    loadCalendar(new Date(calMonth.getFullYear(), calMonth.getMonth() - 1, 1)));
  $('calNext').addEventListener('click', () =>
    loadCalendar(new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 1)));

  $('applyPromo').addEventListener('click', () => {
    const code = $('promoCode').value.trim().toUpperCase();
    state.promoCodes = code ? [code] : [];
    $('promoHint').textContent = '';
    doQuote().then(() => {
      const rejected = (state.quote && state.quote.rejected_codes) || [];
      if (rejected.length) {
        $('promoHint').textContent = rejected[0].message;
      } else if (code) {
        const applied = (state.quote.applied_promotions || []).map((p) => p.name).join(', ');
        $('promoHint').textContent = applied ? `Applied: ${applied}` : 'That code did not change your total.';
      }
    });
  });

  // Language selector (§1).
  $('langSelect').addEventListener('change', () => setLanguage($('langSelect').value));

  // Pricing group: Thai / International (§7). Prices appear only after a choice.
  $('pricingGroup').querySelectorAll('input[name="pricingGroup"]').forEach((input) =>
    input.addEventListener('change', () => pickPricingGroup(input.value)));

  $('backToTickets').addEventListener('click', () => {
    gotoStep(2);
    $('panel-tickets').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  // Make a Payment (aside + mobile sticky) opens the review popup (§16, §18).
  $('makePaymentBtn').addEventListener('click', openReview);
  $('stickyPayBtn').addEventListener('click', openReview);

  // Review dialog controls.
  $('reviewClose').addEventListener('click', () => $('reviewDialog').close());
  $('rvEdit').addEventListener('click', () => {
    $('reviewDialog').close();
    $('panel-details').scrollIntoView({ behavior: 'smooth', block: 'start' });
    $('fullName').focus();
  });
  // Pay opens the mandatory PDPA consent flow; consent Accept performs the charge.
  $('rvPay').addEventListener('click', openConsent);

  $('consentAccept').addEventListener('click', pay);
  $('consentCancel').addEventListener('click', () => $('consentDialog').close());
  $('startOver').addEventListener('click', () => {
    state.selectedDate = null; state.pricingGroup = null; state.quantities = {}; state.quote = null; state.promoCodes = [];
    state.paymentTypeId = null; state.paymentMethod = null;
    $('promoCode').value = ''; $('email').value = ''; $('fullName').value = ''; $('phone').value = '';
    togglePayCta(false);
    gotoStep(1); renderSummary(); loadCalendar(new Date());
  });

  $('viewMailbox').addEventListener('click', async () => {
    try {
      const data = await api('/api/staff/mailbox');
      // The HTML e-ticket is opened as its own document rather than injected here:
      // it ships a full stylesheet that would fight this page, and the CSP sets
      // frame-src 'none' so an iframe preview is not available either.
      $('mailboxBody').innerHTML = data.messages.length
        ? data.messages.slice().reverse().map((m) => `
            <div class="msg"><div class="subj">${escapeHtml(m.subject)}</div>
            <div class="muted">To: ${escapeHtml(m.to)}</div><div>${escapeHtml(m.body)}</div>${
              m.has_html && m.message_id
                ? `<p><a href="/mail/${encodeURIComponent(m.message_id)}" target="_blank" rel="noopener">${
                    escapeHtml(t('view_eticket'))}</a></p>`
                : ''
            }</div>`).join('')
        : '<p class="muted">No messages yet.</p>';
      $('mailboxDialog').showModal();
    } catch (e) { toast(e.message, 'error'); }
  });
  $('mailboxClose').addEventListener('click', () => $('mailboxDialog').close());

  $('mRequest').addEventListener('click', requestCode);
  $('mVerify').addEventListener('click', verifyCode);

  $('sLogin').addEventListener('click', staffLogin);
  $('sBookings').addEventListener('click', () => staffCall('/api/staff/bookings', 'GET /api/staff/bookings'));
  $('sAudit').addEventListener('click', () => staffCall('/api/staff/audit', 'GET /api/staff/audit'));
  $('sSettings').addEventListener('click', loadSettings);
  $('gateScan').addEventListener('click', gateScan);
  $('sPosture').addEventListener('click', async () => {
    const data = await api('/api/security/posture');
    $('sOut').textContent = 'Security posture\n\n' + JSON.stringify(data, null, 2);
  });
}

(async function boot() {
  wire();
  gotoStep(1);
  try {
    await loadCsrf();
    await initLanguage();
    await loadVenue();
    await loadCalendar(new Date());
  } catch (e) {
    toast('Could not reach the server. Is it running?', 'error');
  }
})();
