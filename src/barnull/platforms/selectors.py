"""管理画面のセレクタと、画面から生データを取り出す JavaScript（ここに集約する）。

- SM は styled-components。クラス名のハッシュ部分（-sc-xxxx 以降）はビルドで変わるので、
  表示名（Reservation__Container 等）の部分一致で指定する
- 取り出した生データの解釈は parse_sm.py / parse_ib.py（Python、単体テストあり）
- 画面が変わったら docs/runbook.md の「セレクタ変更」の手順で直す
"""

# ======================= スペースマーケット =======================
SM_INBOX_URL = "https://dashboard.spacemarket.com/{host}/inbox"
SM_THREAD_URL = "https://dashboard.spacemarket.com/{host}/inbox/{pid}"
SM_REVIEW_URL = "https://dashboard.spacemarket.com/{host}/inbox/{pid}/review"

SM_ROW = 'a[class*="Reservation__Container"]'
SM_SIDEBAR = '[class*="MainContentsWithSidebarInbox__SidebarWrapper"]'
SM_MAIN = '[class*="DetailPage__PageContents"]'
SM_TEXTAREA = 'textarea[placeholder="メッセージを入力"]'
SM_FILE_INPUT = 'input[type=file][class*="FloatBottomInbox__InputUpload"]'
SM_SEND = '[class*="FloatBottomInbox__ContainerPc"] a'          # テキスト「送信」
SM_REVIEW_STAR = 'a[class*="ReputationStar__Star-"]'
SM_REVIEW_TEXTAREA = '[class*="WriteReviewPage"] textarea'
# レビュー画面の本文側の「レビューの投稿」（<a type="anchor">。星とコメントが入るまで disabled）。
# サイドバーにも同名の <button> があるが、そちらはレビュー画面に移るだけ
SM_REVIEW_SUBMIT = '[class*="WriteReviewPage"] a, [class*="WriteReviewPage"] button'
# 押すと「レビューの投稿／公開後にレビューの編集はできません。レビューを投稿してよろしいですか？」の確認が出る（2026-10-12 実画面で確認）
SM_REVIEW_CONFIRM_TEXT = "レビューを投稿してよろしいですか"
# 星の色（選んだ星は緑、選んでいない星は灰色）を読む
SM_REVIEW_STARS_JS = r"""() => [...document.querySelectorAll('a[class*="ReputationStar__Star-"]')]
  .map(a => getComputedStyle(a.querySelector('i') || a).color)"""
# 確認モーダルは SM 独自の Modal（2026-10-11 実画面で確認。「ファイルの送信」「送信するファイルに間違いはありませんか？」）
SM_MODAL = '[class*="Modal__ChildrenWrapper"], .ant-modal-wrap:not([style*="display: none"]) .ant-modal, [role=dialog]'
# SM のボタンは <a type="anchor" class="ButtonBase__ButtonComponent…"> が多い（role=button にならない）
SM_BUTTON = 'button, a[class*="ButtonBase__ButtonComponent"], a[type=anchor]'

SM_INBOX_JS = r"""() => [...document.querySelectorAll('a[class*="Reservation__Container"]')].map((a, i) => {
  const q = s => ((a.querySelector(s) || {}).innerText || '').trim();
  return {id: (a.getAttribute('href') || '').split('/').pop(), position: i,
    service: q('[class*="HeaderStatus__ServiceTitle"]'), status: q('[class*="HeaderStatus__StatusText"]'),
    substatus: q('[class*="Reservation__SubStatusContainer"]'), datetime: q('[class*="Schedule__DateTimeText"]'),
    guest: q('[class*="ListItemUserInfo__UserNameContainer"]'), price: q('[class*="Price__Container"]')};
})"""

SM_THREAD_JS = r"""() => {
  const side = document.querySelector('[class*="MainContentsWithSidebarInbox__SidebarWrapper"]');
  const main = document.querySelector('[class*="DetailPage__PageContents"]');
  // 日付の見出し（10月4日／昨日／今日）と各発言を、画面の並び順に読む
  const sel = '[class*="StatusDate__Date"],[class*="MessageSelf__Wrapper"],[class*="MessageOpponent__Wrapper"],' +
              '[class*="SendFileSelf__Wrapper"],[class*="SendFileOpponent__Wrapper"],[class*="SendImageSelf__Wrapper"],' +
              '[class*="SendImageOpponent__Wrapper"],[class*="Status__Content"],' +
              '[class*="RoomConditionCheck__Card"]';
  const timeOf = (e, body) => {
    const sec = e.closest('[class*="MessagesForm__Section"]') || e;
    let t = sec.innerText || '';
    if (body) t = t.replace(body, ' ');
    const m = t.match(/(^|\D)(\d{1,2}:\d{2})(\D|$)/);
    return m ? m[2] : null;
  };
  const items = [];
  let date = null;
  if (main) for (const e of main.querySelectorAll(sel)) {
    const c = String(e.className);
    if (c.includes('StatusDate__Date')) { date = (e.innerText || '').trim(); continue; }
    let kind, text = '', files = [];
    const fileNames = () => [...e.querySelectorAll('[class*="FileTitle"], [class*="ThumbnailTitle"], a')].map(x => (x.innerText || '').trim())
                              .filter((v, i, a) => v && a.indexOf(v) === i);
    if (c.includes('RoomConditionCheck__Card')) { kind = 'exit'; text = (e.innerText || '').trim(); }
    else if (c.includes('SendFileSelf') || c.includes('SendImageSelf')) { kind = 'file_host'; files = fileNames(); }
    else if (c.includes('SendFileOpponent') || c.includes('SendImageOpponent')) { kind = 'file_guest'; files = fileNames(); }
    else if (c.includes('MessageSelf')) {
      kind = 'host'; const m = e.querySelector('[class*="MessageSelf__Message-"]'); text = ((m || e).innerText || '').trim();
    } else if (c.includes('MessageOpponent')) {
      kind = 'guest'; const m = e.querySelector('[class*="MessageOpponent__Message-"]'); text = ((m || e).innerText || '').trim();
    } else { kind = 'system'; text = (e.innerText || '').trim(); }
    items.push({kind, text, files, date, time: timeOf(e, text)});
  }
  const buttons = side ? [...side.querySelectorAll('button, a')].map(b => (b.innerText || '').trim()).filter(Boolean) : [];
  // サイドバーの項目を部品単位で読む（画面の見た目＝CSS に左右されないように）
  const T = (root, s) => { const e = root && root.querySelector(s); return e ? (e.innerText || '').trim() : ''; };
  const fields = {};
  if (side) for (const li of side.querySelectorAll('[class*="styled__ContentListItem"]')) {
    const t = T(li, '[class*="styled__ContentTitle"]');
    const d = li.querySelector('[class*="styled__ContentDesc"]');
    if (t && !(t in fields)) fields[t] = d ? (d.innerText || '').trim() : '';
  }
  const fees = side ? [...side.querySelectorAll('[class*="styled__ContentDescHead"]')].map(h => {
    const b = h.parentElement && h.parentElement.querySelector('[class*="styled__ContentDescBody"]');
    return [(h.innerText || '').trim(), b ? (b.innerText || '').trim() : ''];
  }) : [];
  const labels = side ? [...side.querySelectorAll('[class*="styled__LabelHeader"], [class*="styled__StatusGuide"]')]
                         .map(e => (e.innerText || '').trim()).filter(Boolean) : [];
  const outline = main ? [...main.querySelectorAll('[class*="MessageOutline__ChatOutLineHeadTitle"]')].map(h => {
    const box = h.closest('[class*="MessageOutline__ChatOutLineContent"]') || h.parentElement;
    return [(h.innerText || '').trim(), T(box, '[class*="MessageOutline__ChatOutLineBodyText"]')];
  }) : [];
  const exitRows = main ? [...main.querySelectorAll('[class*="RoomConditionCheck__Row-"]')].map(r =>
    [T(r, '[class*="RoomConditionCheck__HeadTitle"]'),
     ((r.innerText || '').replace(T(r, '[class*="RoomConditionCheck__HeadTitle"]'), '')).trim()]) : [];
  return {url: location.href, sidebar: side ? side.innerText : '', main: main ? main.innerText : '', items, buttons,
          status: T(side, '[class*="HeaderStatus__StatusText"]'), service: T(side, '[class*="HeaderStatus__ServiceTitle"]'),
          guest: T(side, '[class*="styled__GuestName"]'), fields, fees, labels, outline, exit_rows: exitRows};
}"""

# ======================= インスタベース =======================
IB_BOOKINGS_URL = "https://www.instabase.jp/partners/bookings"
IB_BOOKING_URL = "https://www.instabase.jp/partners/bookings/{pid}"
IB_INBOX_URL = "https://www.instabase.jp/partners/inbox"
IB_INBOX_UNREPLIED_URL = "https://www.instabase.jp/partners/inbox?message_filter%5Bunreplied%5D=1"
IB_THREAD_URL = "https://www.instabase.jp/partners/inbox/{tid}"
IB_REVIEW_URL = "https://www.instabase.jp/partners/bookings/{pid}/user_reviews/new"
IB_REVIEW_CREATABLES_URL = "https://www.instabase.jp/partners/bookings/user_review_creatables"
IB_REVIEW_POSTED_URL = "https://www.instabase.jp/partners/bookings/has_user_reviews"

IB_MSG_FORM = 'form[action="/partners/messages"]'
IB_MSG_TEXTAREA = "#message_context"
IB_MSG_FILE = "#message_file"
IB_MSG_SUBMIT = 'form[action="/partners/messages"] input[type=submit][name=commit]'
IB_REVIEW_POINT = "select#user_review_point"
IB_REVIEW_COMMENT = "textarea#user_review_comment"
IB_REVIEW_SUBMIT = 'input[type=submit][name=commit][value="レビューを投稿する"]'

IB_BOOKINGS_JS = r"""() => [...document.querySelectorAll('tr.partner-bookings-table-body-row')].map((tr, i) => {
  const td = [...tr.querySelectorAll(':scope > td')];
  const t = k => td[k] ? td[k].innerText.replace(/\s+/g, ' ').trim() : '';
  const user = tr.querySelector('a[href^="/partners/users/"]');
  return {id: t(0), position: i, space: t(1), status: t(2), guest: user ? user.innerText.trim() : t(3),
          guest_cell: t(3), guest_id: user ? (user.getAttribute('href').match(/users\/(\d+)/) || [])[1] : null,
          datetime: t(4), price: t(5)};
})"""

IB_DETAIL_JS = r"""() => {
  const rows = [...document.querySelectorAll('tr')].map(tr => [...tr.querySelectorAll('th, td')].map(c => c.innerText.trim()));
  const msgBtn = [...document.querySelectorAll('button')].find(b => /メッセージ/.test(b.innerText || ''));
  return {url: location.href, title: document.title, text: (document.querySelector('main') || document.body).innerText, rows,
          message_button: msgBtn ? msgBtn.innerText.trim() : null};
}"""

IB_THREAD_JS = r"""() => {
  const items = [...document.querySelectorAll('div.flex.mt-6')].map(e => ({
    host: e.classList.contains('justify-end'), text: (e.innerText || '').trim(),
    files: [...e.querySelectorAll('a')].filter(a => /attached-document/.test(a.getAttribute('href') || '')
             || /\.(pdf|png|jpe?g|gif|webp|svg|docx?|xlsx?)$/i.test((a.innerText || '').trim()))
           .map(a => (a.innerText || '').trim()).filter(Boolean)
  })).filter(x => x.text);
  const form = document.querySelector('form[action="/partners/messages"]');
  const tid = form ? (form.querySelector('input[name="message_thread_id"]') || {}).value : null;
  return {url: location.href, title: document.title, items, thread_id: tid,
          text: (document.querySelector('main') || document.body).innerText};
}"""

IB_INBOX_JS = r"""() => [...document.querySelectorAll('a[href*="/partners/inbox/"]')].map((a, i) => ({
  thread_id: (a.getAttribute('href').match(/inbox\/(\d+)/) || [])[1], position: i,
  text: (a.innerText || '').trim(), opened: /\[開封済\]/.test(a.innerText || '')
})).filter(x => x.thread_id)"""

IB_UNREAD_JS = r"""() => { const m = document.body.innerText.match(/未読\s*(\d+)\s*件/); return m ? +m[1] : 0; }"""
