from __future__ import annotations
import os, shutil, sqlite3, sys, traceback, zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME="Controle de Importações FAPESP"; APP_VERSION="1.0.2"
BASE=Path(os.environ.get("LOCALAPPDATA",Path.home())) if sys.platform=="win32" else Path.home()/".local/share"
DATA_DIR=BASE/"ControleImportacoesFAPESP_Cadastro"; DB_PATH=DATA_DIR/"cadastro.db"
OLD_DB=BASE/"ControleImportacoesFAPESP"/"controle_importacoes.db"
STATUSES={"preparation":"Em preparação","waiting_documents":"Aguardando documentos","waiting_signature":"Aguardando assinatura","ready":"Pronta para envio","submitted":"Enviada à FAPESP","analysis":"Em análise","diligence":"Em diligência","authorized":"Autorizada","execution":"Em execução","completed":"Concluída","cancelled":"Cancelada"}
STATUS_KEYS={v:k for k,v in STATUSES.items()}

def connect():
    c=sqlite3.connect(DB_PATH,timeout=10); c.row_factory=sqlite3.Row; return c

def init_db():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    with connect() as c:c.executescript("""PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS imports(id INTEGER PRIMARY KEY,process_number TEXT NOT NULL COLLATE NOCASE,researcher_name TEXT NOT NULL COLLATE NOCASE,exporter TEXT NOT NULL COLLATE NOCASE,manufacturer TEXT NOT NULL DEFAULT '',representative TEXT NOT NULL DEFAULT '',proforma_number TEXT NOT NULL DEFAULT '',currency TEXT NOT NULL DEFAULT 'USD',value_amount TEXT NOT NULL DEFAULT '0',opened_on TEXT NOT NULL,nature TEXT NOT NULL DEFAULT 'goods',price_mode TEXT NOT NULL DEFAULT 'three_quotes',status TEXT NOT NULL DEFAULT 'preparation',next_action TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',needs_review INTEGER NOT NULL DEFAULT 0,legacy_id INTEGER UNIQUE,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_process ON imports(process_number);CREATE INDEX IF NOT EXISTS ix_researcher ON imports(researcher_name);CREATE INDEX IF NOT EXISTS ix_exporter ON imports(exporter);CREATE INDEX IF NOT EXISTS ix_status ON imports(status);CREATE INDEX IF NOT EXISTS ix_date ON imports(opened_on DESC,id DESC);""")
    # A abertura do programa nunca deve depender do banco do aplicativo antigo.
    return 0

def migrate_old():
    if not OLD_DB.exists():return 0
    stamp=datetime.now().replace(microsecond=0).isoformat()
    try:
        with connect() as c:
            c.execute("ATTACH DATABASE ? AS old",(str(OLD_DB),)); before=c.total_changes
            c.execute("""INSERT OR IGNORE INTO imports(process_number,researcher_name,exporter,manufacturer,representative,proforma_number,currency,value_amount,opened_on,nature,price_mode,status,next_action,notes,needs_review,legacy_id,created_at,updated_at)
            SELECT p.number,r.name,i.exporter,i.manufacturer,i.representative,i.proforma_number,i.currency,i.value_amount,i.opened_on,i.nature,i.price_mode,i.status,i.next_action,i.notes,COALESCE(i.needs_review,0),i.id,COALESCE(i.created_at,?),COALESCE(i.updated_at,?) FROM old.imports i JOIN old.processes p ON p.id=i.process_id JOIN old.researchers r ON r.id=p.researcher_id""",(stamp,stamp)); n=c.total_changes-before; c.commit(); return n
    except sqlite3.Error:return 0

def decimal_text(value):
    s=str(value).strip().replace(" ","")
    if "," in s and "." in s:s=s.replace(".","").replace(",",".") if s.rfind(",")>s.rfind(".") else s.replace(",","")
    elif "," in s:s=s.replace(".","").replace(",",".")
    try:return format(Decimal(s or "0"),"f")
    except InvalidOperation:raise ValueError("Valor inválido")

def money(value,currency):
    try:s=f"{Decimal(value):,.2f}".replace(",","X").replace(".",",").replace("X",".")
    except InvalidOperation:s=str(value)
    return f"{currency} {s}"

class Form(tk.Toplevel):
    def __init__(self,app,row=None):
        super().__init__(app); self.app=app; self.row=row; self.title("Editar importação" if row else "Nova importação"); self.geometry("760x670"); self.minsize(680,600); self.transient(app); self.grab_set()
        body=ttk.Frame(self,padding=18); body.pack(fill="both",expand=True); sug=app.suggestions(); self.vars={}
        def add(label,name,values=None):
            line=ttk.Frame(body); line.pack(fill="x",pady=5); ttk.Label(line,text=label,width=25).pack(side="left"); v=tk.StringVar(); self.vars[name]=v
            w=ttk.Combobox(line,textvariable=v,values=values,width=45) if values is not None else ttk.Entry(line,textvariable=v,width=45); w.pack(fill="x",expand=True)
        add("Pesquisador *","researcher_name",sug["researchers"]); add("Processo FAPESP *","process_number",sug["processes"]); add("Exportador ou prestador *","exporter",sug["exporters"]); add("Fabricante","manufacturer",sug["manufacturers"]); add("Representante no Brasil","representative",sug["representatives"]); add("Número da proforma","proforma_number"); add("Moeda *","currency",["USD","EUR","GBP","JPY","CHF","Outra"]); add("Valor *","value_amount"); add("Data de abertura","opened_on"); add("Natureza","nature",["Bens ou insumos","Serviço no exterior"]); add("Formação do preço","price_mode",["Três ou mais propostas","Menos de três propostas"]); add("Situação","status",list(STATUSES.values())); add("Próxima providência","next_action")
        ttk.Label(body,text="Observações").pack(anchor="w",pady=(8,2)); self.notes=tk.Text(body,height=5,wrap="word"); self.notes.pack(fill="both",expand=True)
        buttons=ttk.Frame(body); buttons.pack(fill="x",pady=(12,0)); ttk.Button(buttons,text="Cancelar",command=self.destroy).pack(side="right"); ttk.Button(buttons,text="Salvar",command=self.save).pack(side="right",padx=8)
        defaults={"currency":"USD","value_amount":"0,00","opened_on":date.today().isoformat(),"nature":"Bens ou insumos","price_mode":"Três ou mais propostas","status":"Em preparação"}
        for k,v in defaults.items():self.vars[k].set(v)
        if row:
            for k,v in self.vars.items():
                x=row[k]
                if k=="nature":x="Serviço no exterior" if x=="service" else "Bens ou insumos"
                elif k=="price_mode":x="Menos de três propostas" if x=="less_than_three" else "Três ou mais propostas"
                elif k=="status":x=STATUSES.get(x,x)
                elif k=="value_amount":x=str(x).replace(".",",")
                v.set(x or "")
            self.notes.insert("1.0",row["notes"] or "")
    def save(self):
        d={k:v.get().strip() for k,v in self.vars.items()}
        if not all(d[k] for k in ("researcher_name","process_number","exporter")):return messagebox.showerror("Campos obrigatórios","Preencha pesquisador, processo e exportador.",parent=self)
        try:d["value_amount"]=decimal_text(d["value_amount"])
        except ValueError as e:return messagebox.showerror("Valor",str(e),parent=self)
        d["nature"]="service" if d["nature"]=="Serviço no exterior" else "goods"; d["price_mode"]="less_than_three" if d["price_mode"]=="Menos de três propostas" else "three_quotes"; d["status"]=STATUS_KEYS.get(d["status"],"preparation"); d["notes"]=self.notes.get("1.0","end").strip(); stamp=datetime.now().replace(microsecond=0).isoformat()
        cols=["process_number","researcher_name","exporter","manufacturer","representative","proforma_number","currency","value_amount","opened_on","nature","price_mode","status","next_action","notes"]
        with connect() as c:
            if self.row:c.execute(f"UPDATE imports SET {','.join(x+'=?' for x in cols)},needs_review=0,updated_at=? WHERE id=?",[d[x] for x in cols]+[stamp,self.row["id"]])
            else:c.execute(f"INSERT INTO imports({','.join(cols)},created_at,updated_at) VALUES({','.join('?' for _ in cols)},?,?)",[d[x] for x in cols]+[stamp,stamp])
        self.destroy(); self.app.refresh()

class App(tk.Tk):
    def __init__(self,migrated=0):
        super().__init__(); self.title(f"{APP_NAME} {APP_VERSION}"); self.geometry("1260x760"); self.minsize(980,620)
        style=ttk.Style(self); style.theme_use("vista" if "vista" in style.theme_names() else "clam"); style.configure("Treeview",rowheight=29,font=("Segoe UI",10)); style.configure("Treeview.Heading",font=("Segoe UI",10,"bold"))
        top=ttk.Frame(self,padding=(18,14)); top.pack(fill="x"); ttk.Label(top,text=APP_NAME,font=("Segoe UI",18,"bold")).pack(side="left"); ttk.Label(top,text=f"Versão {APP_VERSION}").pack(side="right")
        self.tabs=ttk.Notebook(self); self.tabs.pack(fill="both",expand=True,padx=16,pady=(0,16)); self.p_tab=ttk.Frame(self.tabs,padding=12); self.i_tab=ttk.Frame(self.tabs,padding=12); self.c_tab=ttk.Frame(self.tabs,padding=18); self.tabs.add(self.p_tab,text="Processos"); self.tabs.add(self.i_tab,text="Importações"); self.tabs.add(self.c_tab,text="Configurações")
        self.build_processes(); self.build_imports(); self.build_config(); self.refresh()
    def build_processes(self):
        bar=ttk.Frame(self.p_tab); bar.pack(fill="x",pady=(0,10)); ttk.Label(bar,text="Pesquisar processo, pesquisador ou fornecedor:").pack(side="left"); self.p_search=tk.StringVar(); ttk.Entry(bar,textvariable=self.p_search).pack(side="left",fill="x",expand=True,padx=8); self.p_search.trace_add("write",lambda *_:self.after_idle(self.load_processes))
        cols=("process","researcher","count","totals"); self.p_tree=ttk.Treeview(self.p_tab,columns=cols,show="headings")
        for c,t,w in [("process","Processo",170),("researcher","Pesquisador",300),("count","Importações",110),("totals","Totais autorizados/concluídos",430)]:self.p_tree.heading(c,text=t);self.p_tree.column(c,width=w,anchor="w")
        self.p_tree.pack(fill="both",expand=True); self.p_tree.bind("<Double-1>",self.open_process); ttk.Label(self.p_tab,text="Clique duas vezes em um processo para ver suas importações.").pack(anchor="w",pady=(8,0))
    def build_imports(self):
        bar=ttk.Frame(self.i_tab); bar.pack(fill="x",pady=(0,10)); self.i_search=tk.StringVar(); ttk.Entry(bar,textvariable=self.i_search,width=55).pack(side="left",fill="x",expand=True); self.status=tk.StringVar(value="Todas as situações"); ttk.Combobox(bar,textvariable=self.status,values=["Todas as situações",*STATUSES.values()],state="readonly",width=25).pack(side="left",padx=8); ttk.Button(bar,text="+ Nova importação",command=lambda:Form(self)).pack(side="right"); self.i_search.trace_add("write",lambda *_:self.after_idle(self.load_imports)); self.status.trace_add("write",lambda *_:self.after_idle(self.load_imports))
        cols=("exporter","process","researcher","value","status","next"); self.i_tree=ttk.Treeview(self.i_tab,columns=cols,show="headings",selectmode="browse")
        for c,t,w in [("exporter","Fornecedor / proforma",270),("process","Processo",150),("researcher","Pesquisador",220),("value","Valor",130),("status","Situação",150),("next","Próxima providência",260)]:self.i_tree.heading(c,text=t);self.i_tree.column(c,width=w,anchor="w")
        self.i_tree.pack(fill="both",expand=True); self.i_tree.bind("<Double-1>",lambda _:self.edit()); actions=ttk.Frame(self.i_tab); actions.pack(fill="x",pady=(10,0)); ttk.Button(actions,text="Editar",command=self.edit).pack(side="left"); ttk.Button(actions,text="Excluir",command=self.delete).pack(side="left",padx=8); self.count=ttk.Label(actions); self.count.pack(side="right")
    def build_config(self):
        ttk.Label(self.c_tab,text="Segurança dos dados",font=("Segoe UI",14,"bold")).pack(anchor="w"); ttk.Label(self.c_tab,text="O backup contém somente cadastros. Este aplicativo não armazena anexos.").pack(anchor="w",pady=(4,16)); ttk.Button(self.c_tab,text="Criar backup",command=self.backup).pack(anchor="w",pady=5); ttk.Button(self.c_tab,text="Restaurar backup",command=self.restore).pack(anchor="w",pady=5); ttk.Label(self.c_tab,text=f"Banco: {DB_PATH}",wraplength=900).pack(anchor="w",pady=(24,0))
        ttk.Separator(self.c_tab).pack(fill="x",pady=22)
        ttk.Label(self.c_tab,text="Histórico do aplicativo anterior",font=("Segoe UI",14,"bold")).pack(anchor="w")
        ttk.Label(self.c_tab,text="A importação é opcional e traz somente os cadastros. Nenhum PDF ou anexo será lido.").pack(anchor="w",pady=(4,10))
        state="normal" if OLD_DB.exists() else "disabled"
        ttk.Button(self.c_tab,text="Importar cadastros antigos",command=self.import_old,state=state).pack(anchor="w")
        if not OLD_DB.exists():ttk.Label(self.c_tab,text="Banco antigo não encontrado neste computador.").pack(anchor="w",pady=(6,0))
    def import_old(self):
        if not messagebox.askyesno("Importar histórico","Importar agora os cadastros do aplicativo anterior?\n\nOs anexos não serão carregados."):return
        self.config(cursor="wait"); self.update_idletasks()
        try:
            n=migrate_old(); self.refresh(); messagebox.showinfo("Histórico",f"{n} cadastro(s) novo(s) importado(s).")
        except Exception as e:messagebox.showerror("Histórico",f"Não foi possível importar o histórico.\n\n{e}")
        finally:self.config(cursor="")
    def suggestions(self):
        with connect() as c:
            get=lambda f:[r[0] for r in c.execute(f"SELECT DISTINCT {f} FROM imports WHERE TRIM({f})<>'' ORDER BY {f} COLLATE NOCASE")]
            return {"researchers":get("researcher_name"),"processes":get("process_number"),"exporters":get("exporter"),"manufacturers":get("manufacturer"),"representatives":get("representative")}
    def refresh(self):self.load_processes();self.load_imports()
    def load_processes(self):
        q=f"%{self.p_search.get().strip()}%"
        with connect() as c:
            rows=c.execute("SELECT process_number,researcher_name,COUNT(*) n FROM imports WHERE process_number LIKE ? OR researcher_name LIKE ? OR exporter LIKE ? GROUP BY process_number,researcher_name ORDER BY process_number DESC LIMIT 500",(q,q,q)).fetchall(); self.p_tree.delete(*self.p_tree.get_children())
            for r in rows:
                totals=c.execute("SELECT currency,SUM(CAST(value_amount AS REAL)) FROM imports WHERE process_number=? AND status IN ('authorized','execution','completed') GROUP BY currency",(r["process_number"],)).fetchall(); text=" • ".join(money(str(v),cur) for cur,v in totals) or "Sem valores autorizados"; self.p_tree.insert("","end",values=(r["process_number"],r["researcher_name"],r["n"],text))
    def load_imports(self):
        q=f"%{self.i_search.get().strip()}%"; st=STATUS_KEYS.get(self.status.get(),""); where="(process_number LIKE ? OR researcher_name LIKE ? OR exporter LIKE ? OR manufacturer LIKE ? OR representative LIKE ? OR proforma_number LIKE ?)"; params=[q]*6
        if st:where+=" AND status=?";params.append(st)
        with connect() as c:rows=c.execute(f"SELECT * FROM imports WHERE {where} ORDER BY opened_on DESC,id DESC LIMIT 1000",params).fetchall()
        self.i_tree.delete(*self.i_tree.get_children())
        for r in rows:self.i_tree.insert("","end",iid=str(r["id"]),values=(r["exporter"]+(f" — {r['proforma_number']}" if r["proforma_number"] else ""),r["process_number"],r["researcher_name"],money(r["value_amount"],r["currency"]),STATUSES.get(r["status"],r["status"]),r["next_action"] or "—"))
        self.count.config(text=f"{len(rows)} importação(ões)")
    def open_process(self,_=None):
        x=self.p_tree.focus()
        if x:self.i_search.set(self.p_tree.item(x,"values")[0]);self.tabs.select(self.i_tab)
    def selected(self):
        x=self.i_tree.focus()
        if not x:return None
        with connect() as c:return c.execute("SELECT * FROM imports WHERE id=?",(int(x),)).fetchone()
    def edit(self):
        r=self.selected(); Form(self,r) if r else messagebox.showinfo("Editar","Selecione uma importação.")
    def delete(self):
        r=self.selected()
        if not r:return messagebox.showinfo("Excluir","Selecione uma importação.")
        if messagebox.askyesno("Excluir importação",f"Excluir definitivamente o cadastro de {r['exporter']}?"):
            with connect() as c:c.execute("DELETE FROM imports WHERE id=?",(r["id"],))
            self.refresh()
    def backup(self):
        target=filedialog.asksaveasfilename(title="Salvar backup",defaultextension=".zip",initialfile=f"Controle_Importacoes_backup_{datetime.now():%Y%m%d_%H%M%S}.zip",filetypes=[("Arquivo ZIP","*.zip")])
        if target:
            with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as z:z.write(DB_PATH,"cadastro.db")
            messagebox.showinfo("Backup",f"Backup salvo em:\n{target}")
    def restore(self):
        source=filedialog.askopenfilename(title="Restaurar backup",filetypes=[("Arquivo ZIP","*.zip")])
        if not source or not messagebox.askyesno("Restaurar backup","Substituir os cadastros atuais pelo backup?"):return
        safe=DB_PATH.with_name(f"antes_restauracao_{datetime.now():%Y%m%d_%H%M%S}.db");shutil.copy2(DB_PATH,safe)
        try:
            with zipfile.ZipFile(source) as z:name=next(n for n in z.namelist() if n.endswith(".db"));DB_PATH.write_bytes(z.read(name))
            self.refresh();messagebox.showinfo("Restauração","Backup restaurado.")
        except Exception as e:shutil.copy2(safe,DB_PATH);messagebox.showerror("Restauração",str(e))

def main():
    try:
        init_db(); App().mainloop()
    except Exception:
        DATA_DIR.mkdir(parents=True,exist_ok=True)
        log=DATA_DIR/"erro_ao_abrir.txt"
        log.write_text(traceback.format_exc(),encoding="utf-8")
        try:messagebox.showerror("Erro ao abrir",f"O aplicativo não conseguiu abrir.\n\nFoi criado o diagnóstico:\n{log}")
        except Exception:pass
if __name__=="__main__":main()
