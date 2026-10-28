package uk.superbebra.afzopenflux;

import android.app.Activity;
import android.content.Intent;
import android.content.ClipboardManager;
import android.content.ClipData;
import android.graphics.Color;
import android.net.Uri;
import android.net.VpnService;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.*;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import org.json.JSONObject;
import mobile.Mobile;

public final class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView status;
    private TextView profileStatus;
    private EditText document;
    private Button connect;
    private String pendingURL;
    private final Runnable update = new Runnable() {
        @Override public void run() {
            status.setText(TunnelService.statusText);
            connect.setText(TunnelService.running ? "Отключить" : "Подключить");
            handler.postDelayed(this, 1000);
        }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        go.Seq.setContext(getApplicationContext());
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        int pad = (int)(24 * getResources().getDisplayMetrics().density);
        layout.setPadding(pad, pad*2, pad, pad);
        layout.setBackgroundColor(Color.rgb(246,249,247));
        ScrollView scroll = new ScrollView(this); scroll.addView(layout); setContentView(scroll);
        TextView title = text("AFZ OpenFlux", 30); title.setTextColor(Color.rgb(8,127,91)); layout.addView(title);
        layout.addView(text("Тестовое подключение через Яндекс", 16));
        status = text("Отключено", 20); status.setPadding(0,pad,0,pad); layout.addView(status);
        profileStatus = text(getPreferences(MODE_PRIVATE).getString("document_url", "").isEmpty()
            ? "В боте нажмите «Добавить в AFZ OpenFlux»" : "Профиль сохранён", 16);
        layout.addView(profileStatus);
        button("Вставить из буфера", layout).setOnClickListener(v -> {
            ClipboardManager clipboard = (ClipboardManager)getSystemService(CLIPBOARD_SERVICE);
            ClipData clip = clipboard.getPrimaryClip();
            if (clip == null || clip.getItemCount() == 0 || clip.getItemAt(0).getText() == null) {
                toast("В боте нажмите «Скопировать настройки»"); return;
            }
            importLink(clip.getItemAt(0).getText().toString());
        });
        Button importButton = button("Выбрать файл профиля (JSON)", layout);
        importButton.setOnClickListener(v -> {
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("*/*").addCategory(Intent.CATEGORY_OPENABLE);
            startActivityForResult(intent, 2);
        });
        layout.addView(text("Настройки из бота", 14));
        document = new EditText(this); document.setSingleLine(true);
        document.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        document.setHint("https://disk.yandex.ru/i/…");
        document.setText(getPreferences(MODE_PRIVATE).getString("document_url", ""));
        layout.addView(document);
        connect = button("Подключить",layout);
        connect.setOnClickListener(v -> {
            if (TunnelService.running) {
                startService(new Intent(this,TunnelService.class).setAction("stop")); return;
            }
            pendingURL=document.getText().toString().trim();
            if (!Mobile.validateURL(pendingURL)) {toast("Нужна ссылка на документ Яндекса из профиля бота");return;}
            getPreferences(MODE_PRIVATE).edit().putString("document_url",pendingURL).apply();
            Intent permission=VpnService.prepare(this);
            if(permission!=null) startActivityForResult(permission,1); else startVPN();
        });
        button("Проверить IP в браузере",layout).setOnClickListener(v -> startActivity(new Intent(Intent.ACTION_VIEW,Uri.parse("https://api.ipify.org/"))));
        TextView note=text("При подключении другой VPN на телефоне отключится. Для возврата остановите OpenFlux и включите привычный VPN.\n\nТестовый режим передаёт TCP/IPv4. Используйте HTTPS. Звонки, игры и приложения, требующие UDP, могут не работать. Обход белых списков зависит от сети оператора.\n\nПрофиль предназначен для одного тестового устройства одновременно.",14);
        note.setPadding(0,pad,0,0);layout.addView(note);
        handleImportIntent(getIntent());
        if (android.os.Build.VERSION.SDK_INT>=33 && checkSelfPermission("android.permission.POST_NOTIFICATIONS")!=android.content.pm.PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"},3);
    }

    private TextView text(String value,int size){TextView view=new TextView(this);view.setText(value);view.setTextSize(size);return view;}
    private Button button(String label,LinearLayout parent){Button b=new Button(this);b.setText(label);b.setAllCaps(false);parent.addView(b);return b;}
    private void toast(String value){Toast.makeText(this,value,Toast.LENGTH_LONG).show();}
    private void saveProfile(String value){
        document.setText(value);
        getPreferences(MODE_PRIVATE).edit().putString("document_url",value).apply();
        profileStatus.setText(TunnelService.running
            ? "Профиль добавлен. Отключите VPN и подключитесь снова, чтобы применить его."
            : "Профиль добавлен. Нажмите «Подключить».");
        toast("Профиль добавлен");
    }
    private void importLink(String value){
        try { saveProfile(ImportLink.parse(value)); }
        catch(IllegalArgumentException error){ toast("Не удалось добавить профиль. Нажмите «Скопировать настройки» в боте и вставьте ещё раз."); }
    }
    private void handleImportIntent(Intent intent){
        if(intent != null && Intent.ACTION_VIEW.equals(intent.getAction()) && intent.getData()!=null){
            importLink(intent.getData().toString());
            intent.setData(null); // Do not import it again after screen rotation.
        }
    }
    @Override protected void onNewIntent(Intent intent){super.onNewIntent(intent);setIntent(intent);handleImportIntent(intent);}
    private void startVPN(){startForegroundService(new Intent(this,TunnelService.class).setAction("connect").putExtra("document_url",pendingURL));}
    @Override protected void onResume(){super.onResume();handler.post(update);}
    @Override protected void onPause(){handler.removeCallbacks(update);super.onPause();}

    @Override protected void onActivityResult(int request,int result,Intent data){
        super.onActivityResult(request,result,data);
        if(request==1){if(result==RESULT_OK && pendingURL!=null)startVPN();return;}
        if(request!=2 || result!=RESULT_OK || data==null || data.getData()==null)return;
        try(InputStream input=getContentResolver().openInputStream(data.getData())){
            ByteArrayOutputStream bytes=new ByteArrayOutputStream();byte[] buffer=new byte[4096];int n;
            while((n=input.read(buffer))!=-1){bytes.write(buffer,0,n);if(bytes.size()>65536)throw new Exception("large profile");}
            JSONObject profile=new JSONObject(bytes.toString("UTF-8"));
            if(profile.getInt("version")!=1)throw new Exception("profile version");
            String value=profile.getString("document_url");
            if(!Mobile.validateURL(value))throw new Exception("URL");
            saveProfile(value);
        }catch(Exception error){toast("Не удалось прочитать профиль. Выберите JSON-файл из раздела OpenFlux в боте.");}
    }
}
