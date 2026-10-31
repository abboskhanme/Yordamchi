# Yordamchi — qo'llanma (dev bot, P1)

Telefonda o'qish uchun. Qo'shtirnoq ichidagi bot javoblari — botning aynan o'zi yozadigan matn.
`{loyiha}`, `{vaqt}` kabi joylar o'rniga haqiqiy qiymat chiqadi.

---

## 1. Boshlash

- **Dev bot** — `@OrvionXBot`. U faqat **"OrvionX Projects"** yopiq guruhida ishlaydi.
- **Shaxsiy bot** — `@OrvionX_AI_Bot`. U siz bilan shaxsiy chatda gaplashadi (11-bo'lim).
- Botlardan **faqat siz** foydalana olasiz. Boshqa odamning xabari e'tiborsiz qoladi.
- Guruhda **anonim admin** sifatida yozmang: unda xabar sizdan emas, `GroupAnonymousBot`dan
  kelgandek ko'rinadi va bot uni tashlab yuboradi.
- Mac doim **zaryadkada va uyg'oq** tursin. Mac uxlasa, bot ham javob bermaydi.

<!-- SCREENSHOT: "OrvionX Projects" guruhi, tepada qadalgan "Onlayn: HH:MM" xabari -->

---

## 2. Qanday yozish kerak

- Loyiha nomini ayting ("BonviHR'da login sahifasini tuzat") yoki ishni tasvirlab bering —
  bot loyihani o'zi topadi. Reply qilish shart emas.
- Bot qaysi loyihaga yuborganini birinchi qatorda aytadi:
  - "→ {loyiha}"
  - "→ {loyiha} (ishlayotgan topshiriqqa qo'shdim)" — ish allaqachon ketayotgan bo'lsa.
- Ikkilansa, bitta savol beradi: "«…» — BonviHR'gami yoki HRMentor'gami?"
  - Javob: `2`, `ikkinchisi`, loyiha nomi yoki `hech biri`.
  - 30 daqiqada javob bo'lmasa, xabar hech qayerga yuborilmaydi.
- **Skrinshot + matn + ovoz** ketma-ket yuborilsa, bitta so'rov hisoblanadi.
  - Rasmdan keyin bot taxminan 10 soniya kutadi; matndan keyin 1–2 soniya.
  - Bir so'rov ko'pi bilan 30 soniya yig'iladi.
- **"kut"** (yoki "kutib tur", "biroz kut") — "hali yana yuboraman" degani, hech qachon "to'xta" emas:
  - bot keyingi rasm, matn yoki ovozni 2 daqiqagacha kutadi va ularni bitta so'rovga qo'shadi;
  - bot "kut"ga 👌 reaksiya qo'yadi — kutayotganini shundan bilasiz;
  - ishlayotgan ish **to'xtamaydi**; 2 daqiqada hech narsa kelmasa, "kut" shunchaki unutiladi.
  - To'xtatish uchun faqat `to'xta`, `stop`, `bas` (pastdagi jadval).
- **"Yozmoqda…"** faqat tez javoblarda (hisobot, `holat?`, sozlamalar) javob kelguncha ko'rinadi; loyiha ishida
  "→ {loyiha}" qatori va jonli qator yetarli. Ish paytida ham ko'rsin desangiz: `sozlama: ack.typing_for_work true`.
- Bot xabaringizni o'qiganini ✓✓ bilan ko'rsatadi: 👀 ni bir lahzaga qo'yib, darhol olib tashlaydi, xabarda
  reaksiya qolmaydi (Telegram'da botlar uchun boshqa yo'l yo'q). O'chirish: `sozlama: ui.read_mark o'chir`.
  Reaksiya doim qolsin desangiz: `sozlama: ui.seen_reaction 👀`. "kut"ga 👌 qo'yiladi.
- Guruhda loyihaga aloqasi yo'q savol ham bersangiz bo'ladi — masalan «Python'da decorator nima?», «bu gapni inglizchaga tarjima qil: …», «1250 ning 12 foizi qancha?». Bot oddiy yordamchi kabi javob beradi va hech qaysi loyihada ish boshlamaydi.
- Savol aniq bir loyiha haqida bo'lsa, loyiha nomini ayting yoki o'sha loyihaning xabariga javob (reply) qiling: «BonviHR'da decorator qanday ishlatilgan?».
- **Oddiy gap**: `salom`, `qandaysan?`, `rahmat` — bot oddiy javob beradi, loyiha yoki holat haqida hech narsa
  qo'shmaydi. `nima qila olasan?` yoki `/yordam` — bot nimalar qila olishini ro'yxat qilib ko'rsatadi.
- **Ovozli xabar** o'zbek, rus va inglizcha aralash bo'lsa ham tushuniladi. Bot uni matnga
  o'girib, aytganingizni qaytaradi: "Ovoz: «…» → {loyiha}".
- Ovozni tushunmasa: "Ovozli xabarni matnga o'gira olmadim: … Matn bilan yozing yoki qayta yuboring."
- **Atamani o'rgatish**: `bu so'zni to'g'ri yoz: OrvionX`
  - Javob: "Endi «OrvionX» so'zini aynan shunday yozaman."
  - Keyingi ovozli xabardan boshlab ishlaydi.

---

## 2a. Mavzular (Topics)

Guruh mavzularga bo'linadi: **General** — umumiy gap, savol, hisobot va sozlamalar uchun; har bir loyihaning esa
**o'z mavzusi** bor. Mavzularni siz ochmaysiz — bot ularni o'zi ochadi.

**Bir martalik sozlash** (telefonda, 1 daqiqa)

1. "OrvionX Projects" guruhini oching → guruh nomiga bosing → **Tahrirlash** (qalam belgisi).
2. **Topics** (Mavzular) ni yoqing → **Saqlash**.
3. **Administratorlar** → `@OrvionXBot` → **Manage topics** (Mavzularni boshqarish) huquqini yoqing → **Saqlash**.
4. Guruhga istalgan xabar yozing (masalan `salom`) — bot guruh endi mavzuli ekanini shundan biladi.
5. Tekshirish (Mac'da): `python -m yordamchi.check` — "Topics yoqilgan" va "bot mavzu ocha oladi" qatorlari yashil.

**Qanday ishlaydi**

- Loyihaga **birinchi ish** berganingizda bot shu loyiha nomi bilan mavzu ochadi (masalan «BonviHR»). Keyingi
  ishlar shu mavzuda davom etadi. Hamma loyihaga birdan mavzu ochilmaydi — faqat ish berilganlariga.
- **General'da** ish bersangiz ("BonviHR'da login sahifasini tuzat"), ish loyihaning mavzusida davom etadi:
  "→ {loyiha}", jonli qator, savollar, qarorlar va natija — hammasi o'sha mavzuda. General'da bitta qator qoladi:
  "→ BonviHR · mavzusida davom etadi" — bossangiz mavzu ochiladi. Shu qatorga reply qilsangiz ham BonviHR'ga ketadi.
- **Loyiha mavzusida** yozgan hamma narsangiz o'sha loyihaga ketadi: loyiha nomini aytish shart emas, bot
  "qaysi loyiha?" deb so'ramaydi. Ish ketayotgan bo'lsa, xabaringiz shu ishga qo'shiladi.
- Mavzuda `to'xta` — **faqat shu loyiha** to'xtaydi, boshqalari ishlashda davom etadi. `davom ettir`,
  `qayta yubor`, `yangi sessiya`, `/hisobot` — ham shu loyiha uchun.
- Mavzudagi `ha`, `yo'q` yoki `2` — faqat shu loyihaning savoli yoki qaroriga javob, boshqa loyihanikiga emas.
- `salom`, `rahmat`, `holat?` — qayerda yozsangiz, o'sha yerda javob keladi. Sozlamalar va `xarajat?` javobi
  General'da.
- Umumiy savol, hisobot va sozlamalarni General'da yozing. Mavzuda «BonviHR qanday?» desangiz, bu loyihaga topshiriq
  bo'ladi (agent javob beradi); mavzu ichida tezkor hisobot uchun `/hisobot` deng.
- Mavzuni **yopsangiz**, loyihaga keyingi ish berilganda bot uni qayta ochadi. **O'chirsangiz**, keyingi xabarda
  yangi mavzu ochadi (eski mavzudagi yozishmalar qaytmaydi).
- Yangi loyiha (`yangi loyiha: …`) hali yo'q (10-bo'lim). Qo'shilganda uning rejasi va qurilishi o'z mavzusida
  bo'ladi.

**Mavzular kerak bo'lmasa**

- Guruhda Topics'ni o'chirsangiz, bot avvalgidek oddiy guruh bo'lib ishlaydi — hammasi bitta chatda.
- Faqat botda o'chirish: `sozlama: topics.enabled false`. Qayta yoqish: `sozlama: topics.enabled true`.
- Botda «Manage topics» huquqi bo'lmasa, ishlar General'da davom etadi va bot bir marta eslatadi: "Loyiha uchun
  mavzu ocha olmadim: botga guruhda **Manage topics** (mavzularni boshqarish) huquqini bering. Hozircha ishlar
  General'da davom etadi."

<!-- SCREENSHOT: General'dagi "→ BonviHR · mavzusida davom etadi" qatori va BonviHR mavzusidagi jonli qator -->

---

## 3. Loyiha ishlari

- Ko'rsatma, qo'shimcha va tuzatishlarni oddiy gap bilan yozing. Ish ketayotganda yozgan
  qo'shimchangiz o'sha ishga qo'shiladi.
- Ish paytida bitta "jonli" qator yangilanib turadi: "{loyiha} · ✍️ Kod yozyapman… · ⏱ 4:12" — bot nima
  qilayotgani va qancha vaqt o'tgani; kod va buyruqlar ko'rsatilmaydi. Batafsil qadamlar kerak bo'lsa:
  `sozlama: ui.progress_steps 3`.
- Oxirida natija alohida xabar bo'lib keladi: Natija, Fayllar, Testlar, Commit, Vaqt.
- **Commit va push avtomatik.** Natijada ko'rinadi:
  - "Commit: {sha} · push: bajarildi ({upstream})"
  - "Commit: commit/push qilinmadi" — agent o'zi qilmasa.

**To'xtatish va yo'nalishni o'zgartirish**

| Nima yozasiz | Nima bo'ladi |
|---|---|
| `to'xta` (`stop`, `bas`) | Ishlayotgan ish to'xtaydi: "{loyiha}: to'xtatdim." |
| `BonviHR'ni to'xtat` yoki `/stop BonviHR` | Aynan shu loyiha to'xtaydi |
| `yo'q, boshqacha qil: <yangi ko'rsatma>` | Joriy ish to'xtaydi, yangi ko'rsatma bilan davom etadi |
| `hammasini to'xtat` | Hamma ishlar to'xtaydi |
| `BonviHR yangi sessiya` | "BonviHR: eski sessiyani yopdim — keyingi topshiriq yangi sessiyada, oxirgi eslatma bilan." |

- Bir nechta ish ketayotgan bo'lsa va loyiha aytilmasa: "Bir nechta ish ketyapti: … Qaysi birini
  to'xtatay? Loyiha nomini yozing." Loyiha nomini yozing yoki uning xabariga reply qiling.
- Hech narsa ishlamayotgan bo'lsa: "Hozir ishlayotgan topshiriq yo'q."
- Boshqa odamdan **forward qilingan** "to'xta" hech narsani to'xtatmaydi.
- **Yangi sessiya**: agent chalkashib qolsa, `BonviHR yangi sessiya` deng. Keyingi topshiriq yangi
  sessiyada, oxirgi ish eslatmasi bilan boshlanadi. Ish ketayotgan bo'lsa, u eski sessiyada tugaydi:
  "BonviHR: hozirgi ish eski sessiyada tugaydi; keyingi topshiriq yangi sessiyada, oxirgi eslatma bilan."

**Noto'g'ri loyihaga ketdi**

1. Yozing: `bu HRMentor uchun edi` (oxirgi 2 soat ichidagi ish uchun).
2. Bot: "→ HRMentor: topshiriqni BonviHR'dan olib, HRMentor'ga qayta yubordim."
3. Bot o'zgargan fayllarni sanab so'raydi: "… Ularni avvalgi holatiga qaytaraymi? (ha / yo'q)"
4. `ha` — qaytaradi, `yo'q` — o'z holicha qoldiradi.

**Loyihalar ro'yxati**

- `loyihalar` yoki `/loyihalar` — hamma loyihalar.
- `BonviHR'ni «bonvi» deb tani` — qisqa nom qo'shish.
- `BonviHR tavsifi: kadrlar tizimi` — tavsif yozish.
- `BonviHR'ni arxivla` / `BonviHR'ni arxivdan chiqar`.
- Bot umuman tegmasligi kerak bo'lgan papka: `sozlama: OvozYoz'ni projects.excluded'ga qo'sh`. Shundan keyin bot uni ko'rmaydi, ro'yxatga olmaydi va unda ishlamaydi. Ro'yxatdan nom olib tashlansa, bot bir marta so'raydi.

<!-- SCREENSHOT: ishlayotgan topshiriqning jonli qatori va natija xabari -->

---

## 4. Savollar va qarorlar

**Agent savoli** shunday keladi: "{loyiha} · Savol: …" va pastida raqamlangan variantlar.

Javob berish usullari (hammasi ishlaydi):

- raqam: `2`, `2-chi`, `ikkinchisi`;
- variant matni: `PostgreSQL` (yoki `postgres`);
- o'z so'zlaringiz;
- ovozli xabar;
- savolga reply.

Bir nechta savol bo'lsa — har biriga alohida qatorda javob yozing. Ko'p tanlovli savolda: `1, 3`.

- **Ikki loyihada savol ochiq bo'lsa**, loyihani ayting: `BonviHR 2`.
  - Aytmasangiz, bot so'raydi: "«2» — BonviHR savoligami yoki HRMentor savoligami?"
- Javob qabul qilinsa: "→ {loyiha}: javob «…»".

**Muhim qarorlar** (deploy, prod, serverda o'zgarish, ma'lumot o'chishi, pul sarfi, force push):

- Bot har bir qaror uchun **bitta** savol beradi:
  "{loyiha} · Muhim qaror #17: … Oqibati: … Bajaraymi? (ha / yo'q / boshqa ko'rsatma)"
- `ha` — "{loyiha} · #17 tasdiqlandi — aynan shu buyruq bir marta bajariladi."
- `yo'q` — "{loyiha} · #17 bekor qilindi, bajarilmaydi."
- Boshqa ko'rsatma yozsangiz — buyruq bajarilmaydi, ko'rsatmangiz agentga boradi.
- Bir nechta qaror ochiq bo'lsa: savolga reply qiling yoki raqamini yozing (`#17 ha`).

**Forward qilingan xabar** hech qachon sizning javobingiz hisoblanmaydi — u faqat material.

<!-- SCREENSHOT: "Muhim qaror #…" savoli va "ha" javobi -->

---

## 5. Hisobot va holat

| Nima yozasiz | Nima olasiz |
|---|---|
| `BonviHR qanday?` yoki `/hisobot BonviHR` | Bitta loyiha: hozirgi ish, oxirgi natija, git, bugungi commit'lar |
| `bugun nima o'zgardi?` yoki `/hisobot` | Bugun o'zgargan hamma loyihalar |
| `holat?` yoki `/holat` | Botning o'zi: Claude CLI, ClaudeManager, ishlar, navbat, Mac xotirasi va diski, oxirgi xatolar |
| `xarajat?` yoki `/xarajat` | Bugun / 7 kun / shu oy: pullik API (limitga nisbatan), Claude obunasi (taxmin), eng faol loyihalar |

- "Ma'lumot: …" qatori faqat kerak bo'lganda chiqadi: ma'lumot eski bo'lsa ("Ma'lumot: 45 daqiqa oldin") yoki
  yangilab bo'lmasa. Har javobda bo'lsin desangiz: `sozlama: report.freshness_line always`.
- Bot sodda, samimiy gaplashadi. Ohangni o'zgartirish: `sozlama: ohanging qisqaroq/rasmiyroq bo'lsin`.
- Hisobot loyiha sessiyasiga tegmaydi va ishlayotgan ishni to'xtatmaydi.
- **Chuqur savol** ("BonviHR'da login nega ishlamayapti?", "X'dagi xatoni tekshir") — bot darhol
  (2 soniya ichida) yozadi: "{loyiha} · Tekshiryapman (~3 daqiqa)", keyin natijani alohida yuboradi.
  Tekshiruv faqat o'qiydi, hech narsani o'zgartirmaydi.
- **Xarajat limiti**: pullik xizmat oylik limitining 80 % iga yetsa ogohlantiradi; 100 % da uning pullik
  chaqiruvlarini to'xtatadi va limitni qanday oshirishni aytadi.

---

## 6. Serverlar

| Nima yozasiz | Nima bo'ladi |
|---|---|
| `bonvi-srv serverini kuzat` | "bonvi-srv: kuzatuvga oldim — har … daqiqada faqat o'qiydigan buyruqlar bilan tekshiraman." |
| `bonvi-srv'ni kuzatma` | "bonvi-srv: kuzatuvdan chiqardim." |
| `serverda nima bo'lyapti?` yoki `bonvi-srv qanday?` | Har server uchun bitta qator, oxirida "Ma'lumot: …" |

- Server nomi — `~/.ssh/config` dagi alias (bitta so'z).
- Yangi tekshiruv kerak bo'lsa, avval "Tekshiryapman (~… daqiqa): …" keladi.
- Muammo topilsa, bot o'zi yozadi: "bonvi-srv serverida muammo: …" (har muammo bir marta).
- Faqat o'tish (jump) uchun belgilangan serverda bot hech qanday buyruq ishlatmaydi.

---

## 7. Sozlamalar

- **Ko'rish**: `sozlamalaring qanday?` (yoki `/sozlamalar`). Qisqa ro'yxat + to'liq ro'yxat faylda.
- **O'zgartirish**: gapni `sozlama:` bilan boshlang (`sozlama: rasmdan keyin 15 soniya kut`) yoki botga
  "sen" deb, o'zi haqida ayting — loyiha nomisiz: `javoblaringni qisqaroq qil`, `ovozingni ayolga o'zgartir`,
  `gaplaringni iliqroq qil`. Bunday so'z bo'lmasa (`rasmdan keyin 15 soniya kut`), `sozlama:` qo'shing.
  - Javob: "Sozlama o'zgardi: … Keyingi xabardan kuchga kiradi."
  - Tushunmasa, bitta aniqlashtiruvchi savol beradi.
  - Loyiha nomi bo'lsa, bu loyiha ishi: `BonviHR'ning sozlamalar sahifasini tuzat` BonviHR'ga ketadi.
  - Sozlama bilan bo'lmaydigan narsa (yangi imkoniyat) uchun bot P2 bosqichidan keyin
    `o'zingga … qo'sh` deyishni taklif qiladi.
- **Qaytarish**: `oldingi holatiga qaytar` (oxirgi o'zgarish) yoki
  `oldingi holatiga qaytar burst.after_media_s` (bitta sozlama).
- **Himoyani susaytiradigan** o'zgarish bitta savol beradi:
  "Sozlamalar · Muhim qaror #…: … Bu himoyani susaytiradi. Qo'llaymi? (ha / yo'q)"

---

## 8. Kalitlar

- Gemini (`AIza…` yoki `AQ.…`) yoki OpenRouter (`sk-or-v1-…`) kalitini guruhga oddiy xabar qilib tashlang.
- Bot:
  1. xabaringizni o'chiradi;
  2. kalitni shifrlab saqlaydi: "Kalit shifrlab saqlandi: …";
  3. ovoz holatini aytadi, masalan: "Ovoz: OpenRouter (Gemini) ulandi."
- Gemini kaliti bo'lsa ovozli xabarlar `gemini-3.5-transcribe-live` modeli bilan matnga o'giriladi
  (Google'ning eng yangi transkripsiya modeli; kunlik so'rov limiti yo'q). Boshqa model kerak bo'lsa:
  `sozlama: stt.google_model gemini-3.5-transcribe`.
- Xabarni o'chira olmasa: "Xabaringizni o'chira olmadim … Kalit chatda qoldi — kalitni
  almashtiring." Bunda kalitni albatta yangisiga almashtiring.
- Claude login tokenini bot qabul qilmaydi — akkauntlarni ClaudeManager boshqaradi.
- Kalitlar hech qachon ochiq matnli faylda saqlanmaydi va log'ga yozilmaydi.

---

## 9. Agar nimadir bo'lsa

**Qadalgan "Onlayn: HH:MM" xabari** — bot tirikligini ko'rsatadi. U har 5 daqiqada yangilanadi.
Vaqt 10 daqiqadan eski bo'lsa — bot ishlamayapti.

**Bot o'zi yozadigan xabarlar**

| Xabar | Ma'nosi |
|---|---|
| "Mac uxlagan edi 02:10–07:45, 3 ta xabar qayta ishlanmoqda" | Mac uxlagan; yozganlaringiz yo'qolmagan |
| "Internet uzilgan edi 14:02–14:20" | Tarmoq uzilgan edi, endi tiklandi |
| "Qayta ishga tushdim (…). Uzilib qolgan ishlar: … — davom ettiraymi?" | Bot qayta yondi; `davom ettir` yoki `BonviHR'ni davom ettir` deb yozing |
| "Qayta ishga tushdim (…). Uzilib qolgan ish yo'q." | Hammasi joyida |
| "{loyiha}: Claude limiti tugadi — … o'zim davom ettiraman." | Hech narsa qilish shart emas |
| "{loyiha}: … — to'xtatib turibman. Davom ettiraymi?" | Ish juda uzoq yoki bir xil xato takrorlandi; `davom ettir` yoki `to'xta` |
| "{loyiha}: oxirgi topshiriq bajarilmay qoldi — qayta yuboraymi?" | `qayta yubor` deb yozing |

**Bot jim bo'lsa**

1. Qadalgan "Onlayn: …" vaqtiga qarang.
2. `holat?` deb yozing.
3. Javob bo'lmasa — Mac'ni tekshiring: yoqiqmi, zaryadkadami, uyg'oqmi, internet bormi.
   Bot 10 daqiqadan beri Telegram'ga ulana olmasa, Mac ekranida "Yordamchi: … Telegram'ga ulana
   olmayapti" degan bildirishnoma chiqadi (har uzilishda bir marta). Internet bo'lsa-yu bildirishnoma
   chiqqan bo'lsa, odatda DNS keshini tozalash yetadi — `docs/DEPLOY.md` §7. Botni qayta ishga tushirish
   shart emas: u ulanishini har 5 daqiqada o'zi yangilaydi.
4. Mac'da hammasi joyida bo'lsa — bot jarayonini `docs/DEPLOY.md` §7 ("How the owner checks
   each thing") bo'yicha tekshiring; qayta ishga tushirish — §4.3.
5. Bot qayta yonganda yozganlaringizni o'zi qayta ishlaydi — qayta yuborish shart emas.

---

## 10. Hali yo'q

Quyidagilarga bot "Bu imkoniyat hali yo'q — P2 bosqichida qo'shiladi." deb javob beradi va
hech qaysi loyihaga yubormaydi:

- integratsiyalar: `integratsiyalar`, `/integratsiyalar`, `X'ga ulan: <manzil>` (shaxsiy botda bor — 11-bo'lim);
- yangi loyiha: `yangi loyiha: …` (shuningdek alohida `yangi loyiha`, `boshla`, `tamom` so'zlari);
- sessiyani Mac'ga o'tkazish: `/sessiya`, `BonviHR sessiyasi`;
- loyihani yopib turish va qaytarish: `BonviHR'ni yopib tur`, `BonviHR'ni davom ettiramiz`;
- botni o'zini yangilash: `o'zingga … qo'sh`.

**Shaxsiy bot** `@OrvionX_AI_Bot` — 11-bo'limda.

---

## 11. Shaxsiy bot (`@OrvionX_AI_Bot`)

Shaxsiy chatda yozing yoki ovozli xabar yuboring — guruh shart emas. Bot faqat sizga javob beradi.

- **Istalgan savol** — matn yoki ovoz bilan. Avval matnli javob keladi (yozilayotganda "yozmoqda…" va
  javob qismlari ko'rinib turadi), keyin ~20–30 soniyalik ovozli xulosa.
  - Javob qisqartirilgan bo'lsa, oxirida "To'liq javob uchun: «to'liqroq»" turadi — `to'liqroq` deb yozing.
  - `yangi suhbat` — oldingi mavzularni unutib, yangidan boshlaydi.
  - `to'xta` — yozilayotgan javobni (va uning ovozini) to'xtatadi.
- **Rasm yoki skrinshot** — yuboring (izoh bilan yoki izohsiz): bot rasmni ko'rib javob beradi.
- **Havola, YouTube, video (≤ 20 MB)** — bot avval "Ko'ryapman…" deydi, keyin nima ekanini, asosiy
  fikrlarini va o'z fikrini aytadi. Faqat havolaning o'zini yuborsangiz, bot ~10 soniya kutadi:
  ortidan savol yozishingiz mumkin. Login yoki pullik sahifani ko'ra olmasa, shuni aytadi.
- **Istalgan fayl** — PDF, Word (`.docx`), Excel (`.xlsx`), PowerPoint (`.pptx`), matn, CSV yoki audio yozuvni
  yuboring (≤ 20 MB). Izohga nima qilishni yozing: "xulosa qil", "tarjima qil", "jadvalga sol", "xatolarini top".
  Izohsiz yuborsangiz, bot fayl nima ekanini va asosiy fikrlarini aytadi. Eski `.doc`/`.xls` va arxivlarni o'qimaydi:
  ularni PDF yoki DOCX qilib yuboring.
- **Yig'ilish yozuvi** (mp3, m4a, wav, ogg… ≤ 20 MB, 2,5 soatgacha) — bot "Tinglayapman…" deydi, yozuvni matnga
  aylantiradi (kim gapirgani va vaqti bilan) va **transkriptni `.txt` fayl qilib** yuboradi. Keyin qisqa xulosa,
  qarorlar va topshiriqlar (kim, nima, qachongacha) yozadi hamda **bayonnomani Word fayl** qilib yuboradi. Izohda
  boshqacha so'rasangiz ("faqat topshiriqlarni Excel'ga", "rus tilida bayonnoma"), shuni qiladi. Buning uchun
  Gemini yoki OpenRouter kaliti kerak (pastda).
- **Hujjat tayyorlash**: "shu bo'yicha xat yozib ber, Word qilib", "jadvalni Excel qilib yubor", "PDF hisobot
  tayyorla" — bot DOCX, PDF, XLSX, MD yoki TXT faylni javobdan keyin yuboradi.
- **Skrinshot va fayllar**: "example.com saytining skrinshoti", "shu sahifadagi rasmni yubor",
  "shu videoni yubor" (YouTube ham) — bot faylni tayyorlab, javobdan keyin yuboradi (≤ 50 MB).
- **Loyiha holati**: "BonviHR qanday?" — dev botning ma'lumotidan o'qiydi, oxirida "Ma'lumot: … oldin".
  Shaxsiy bot loyihalarda hech narsa o'zgartira olmaydi va dev botga buyruq bera olmaydi.
- **Terminaldagidek ishlaydi (agent rejimi)**: shaxsiy bot endi terminaldagi Claude Code kabi ishlaydi — buyruqlarni
  o'zi bajaradi, dastur o'rnatadi, fayl yozadi va tahrirlaydi, API'larga o'zi ulanadi. U `~/Documents/Projects/_shaxsiy`
  papkasida ishlaydi (boshqa joyni aytmasangiz, fayllarni shu yerda yaratadi).
  - Xavfli ishdan oldin (deploy, ma'lumotni o'chirish, pul sarflash, boshqalarga yuborish) bot javobidan keyin
    **bitta savol** beradi: "#1: Claude shu ishni bajarmoqchi: «…»". `ha` desangiz, o'sha ishni bir marta bajaradi va
    davom etadi; `yo'q` desangiz, bajarmaydi va o'rniga nima qilishini aytadi. Boshqa xabar yozsangiz, savol bekor
    bo'ladi ("#1 bajarilmadi").
  - Yordamchining o'z kalitlari va ma'lumotlariga hamda Claude akkaunt fayllariga hech qachon tegmaydi — buni so'rab
    ham bo'lmaydi.
  - `sozlama: agent.enabled false` — botni oddiy yordamchiga qaytaradi (buyruq bajarmaydi). Qayta yoqishda
    (`sozlama: agent.enabled true`) bot `ha` / `yo'q` deb so'raydi.
- `holat?`, `sozlamalaring qanday?`, `xarajat?` — dev botdagidek.
- **Sozlamalar so'z bilan**: "javoblaring qisqaroq bo'lsin", "ovozingni erkakka o'zgartir", "ovozni o'chir",
  "sozlama: …", "oldingi holatiga qaytar". Himoyani susaytiradigan o'zgarish (masalan qimmat model) uchun
  bot bitta savol beradi — `ha` yoki `yo'q` deb javob bering.
- **Kalit**: Gemini (`AIza…` yoki `AQ.…`) yoki OpenRouter (`sk-or-v1-…`) kalitini **shu chatga** bir marta yuboring —
  bot xabarni o'chiradi, kalitni saqlaydi va ovozni sozlaydi. Shaxsiy bot dev botning kalitlarini
  ko'rmaydi: ovozli savollar, video tahlili va zaxira model uchun kalit shu yerda kerak. Kalitsiz ham
  matnli savol-javob ishlaydi, ovozli xulosa esa bepul Edge ovozi bilan keladi.
- **Claude ishlamasa** (limit tugagan va h.k.) javob "(zaxira model) …" deb boshlanadi.

- **Yangiliklar**: "bugungi yangiliklar", "moliya yangiliklari", "texnologiya yangiliklari" yoki `/yangiliklar` — bot
  internetdan qidiradi va 7 tagacha yangilik beradi. Har birida manba (havola bilan) va chiqqan vaqti bor, kurs va
  narxlarda esa manba va sana.
- **Integratsiyalar (API kalit yoki MCP)** — kalitni **shu chatga** yuboring va qaysi xizmat ekanini ayting:
  "mana ob-havo xizmatining API kaliti: kalit: xxxxxxxx, ulan" yoki "Bonvi HR'ga ulan: https://…/mcp". Kalitni
  albatta "kalit: …" deb yozing: bot xabarni o'chiradi va kalitni shifrlab saqlaydi. Claude kalitning o'zini ko'rmaydi.
  Bot xizmat manzilini va kalit qanday yuborilishini o'zi aniqlaydi, bitta sinov so'rovi qiladi va "ulandi" deydi.
  Shundan keyin shu xizmat kerak bo'lgan savolda uni o'zi ishlatadi, kalitni qayta so'ramaydi. API kalitlar faqat
  o'qish uchun ishlatiladi (GET). MCP serverdan esa faqat o'qiydigan vositalar chaqiriladi.
  - Kirish (OAuth) kerak bo'lsa, bot havola yuboradi: oching, ruxsat bering, ochilmay qolgan sahifa manzilini
    (`http://localhost:33418/…`) shu chatga yuboring.
  - `integratsiyalar` — ro'yxati; "X integratsiyasini o'chir"; "X integratsiyasini qaytar" (30 kun ichida);
    "X'ga qayta ulan" (kirish muddati tugasa).
- **Ovoz tanlash**: "ovozing yoqmadi" — bot 3 ta raqamlangan ovoz namunasini yuboradi. Yoqqanini "ikkinchisi" yoki "2"
  deb yozing — keyingi javoblar o'sha ovozda keladi. Boshqa xabar yozsangiz, ovoz o'zgarmaydi. Bepul Edge ovozida
  faqat 2 ta o'zbekcha ovoz bor (ayol va erkak).