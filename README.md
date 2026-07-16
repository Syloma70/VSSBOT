# VSSBOT

Volkan Arslan hesabının Ticarion PC otomasyonuna katılım kararını Telegram üzerinden
7/24 yöneten bağımsız Railway servisidir. BlueStacks ve oyun otomasyonu bu repoda yoktur.

## Telegram komutları

- `/calistir`: Sıradaki PC turuna bir kez katıl; karar PC tarafından alınınca otomatik kapanır.
- `/surekli`: Her PC otomasyon turuna katıl.
- `/iptal`: Tek seferlik ve sürekli bütün çalışma izinlerini kapat.
- `/durum`: Mevcut tercihi göster.
- `/etkinlik [gün]`: Railway'e aktarılan etkinlik ödüllerinin toplam listesi.
- `/etkinlikhesap [gün]`: Etkinlik ödüllerinin hesap bazlı listesi.
- `/yardim`: Kullanım panelini göster.

Komutlar yalnız `VOLKAN_USERNAME` sahibinin ve `ADMIN_USERNAME` yöneticisinin özel
sohbetinden kabul edilir. Varsayılan yönetici `@JackTheRipppper` hesabıdır. İlk başarılı
eşleşmede iki hesabın Telegram kullanıcı kimliği ayrı ayrı kaydedilir ve sonraki komutlarda
kimlik doğrulanır. Her iki yetkili de hesabı açabilir, sürekli moda alabilir, kapatabilir ve
durumunu görebilir.

## Railway kurulumu

1. Bu GitHub reposundan yeni ve ayrı bir Railway servisi oluştur.
2. `TELEGRAM_BOT_TOKEN`, `CONTROL_SECRET`, `VOLKAN_USERNAME=vlknarslan` ve
   `ADMIN_USERNAME=jacktheripppper` değişkenlerini ekle.
3. Kalıcı bir Railway Volume oluşturup `/data` yoluna bağla.
4. Servis için public domain üret. Sağlık kontrolü `GET /health` adresindedir.

Volume kullanılmazsa Railway yeniden dağıtımında kayıtlı mod ve Telegram offset'i kaybolabilir.

## PC karar API'si

PC, her otomatik turdan hemen önce aşağıdaki isteği gönderir:

```http
POST /api/claim
Authorization: Bearer CONTROL_SECRET
```

Örnek yanıt:

```json
{"ok":true,"active":true,"mode":"once","consumed":true,"claimed_at":"..."}
```

`once` kararı bu istek sırasında atomik olarak tüketilir. `always`, `/iptal` gelene kadar
aktif kalır. Salt okunur durum kontrolü `GET /api/status` üzerinden aynı yetkilendirmeyle
yapılabilir.

PC otomasyonu etkinlik sonuçlarını aynı kimlik doğrulamasıyla `POST /api/events`
adresine gönderir. Kayıtlar `external_id` ile tekilleştirilir.
