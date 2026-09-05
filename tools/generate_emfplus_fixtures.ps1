param([string]$OutputDirectory = "tests/fixtures/gdiplus")
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
public static class EmfPlusFixtures {
    [DllImport("gdi32.dll")] static extern bool MoveToEx(IntPtr dc, int x, int y, IntPtr old);
    [DllImport("gdi32.dll")] static extern bool LineTo(IntPtr dc, int x, int y);
    // 绘制包含变换、裁剪、文字、透明图片及样式降级的原生测试场景。
    static void Draw(Graphics g, string scene) {
        g.PageUnit = GraphicsUnit.Pixel;
        g.Clear(Color.White);
        if (scene == "geometry") {
            using (var pen = new Pen(Color.Blue, 3)) {
                pen.DashStyle = DashStyle.Dash;
                g.DrawRectangle(pen, 12, 12, 80, 50);
                g.FillEllipse(Brushes.Red, 110, 12, 60, 50);
                g.DrawArc(Pens.Black, 190, 12, 60, 50, 30, 220);
                g.FillPie(Brushes.Green, 12, 90, 60, 60, 0, 270);
                g.DrawBezier(pen, 100, 110, 140, 50, 180, 180, 240, 110);
            }
            using (var path = new GraphicsPath()) {
                path.AddPolygon(new PointF[] { new PointF(25,190), new PointF(80,165), new PointF(90,220) });
                g.FillPath(Brushes.Purple, path);
                g.DrawPath(Pens.Black, path);
            }
        } else if (scene == "state") {
            var saved = g.Save();
            g.TranslateTransform(45, 30);
            g.RotateTransform(20);
            g.SetClip(new Rectangle(0,0,100,70));
            using (var brush = new SolidBrush(Color.FromArgb(150,255,0,0))) g.FillRectangle(brush, -20,-20,170,100);
            g.Restore(saved);
            g.FillRectangle(Brushes.Blue, 180,20,30,40);
            var container = g.BeginContainer(new RectangleF(120,130,100,80), new RectangleF(0,0,50,40), GraphicsUnit.Pixel);
            g.FillRectangle(Brushes.Green, 5,5,25,20);
            g.EndContainer(container);
        } else if (scene == "images") {
            using (var image = new Bitmap(20,20,PixelFormat.Format32bppArgb)) {
                using (var ig = Graphics.FromImage(image)) {
                    ig.Clear(Color.Transparent);
                    ig.FillRectangle(Brushes.Red,0,0,10,10);
                    using (var brush = new SolidBrush(Color.FromArgb(128,0,180,0))) ig.FillRectangle(brush,10,0,10,20);
                }
                g.DrawImage(image, new Rectangle(20,20,100,100),0,0,20,20,GraphicsUnit.Pixel);
                g.DrawImage(image, new PointF[] {new PointF(145,20),new PointF(245,45),new PointF(125,120)},new RectangleF(0,0,20,20),GraphicsUnit.Pixel);
            }
        } else if (scene == "text") {
            using (var font = new Font("Arial", 16, FontStyle.Regular, GraphicsUnit.Pixel)) {
                g.DrawString("Hello EMF+\nSecond line",font,Brushes.Black,new RectangleF(15,15,220,70),StringFormat.GenericTypographic);
                var fmt = new StringFormat(); fmt.Alignment = StringAlignment.Center;
                g.DrawString("Centered",font,Brushes.Blue,new RectangleF(20,130,200,50),fmt);
                fmt.Dispose();
            }
        } else if (scene == "fallback") {
            using (var gradient = new LinearGradientBrush(new Rectangle(20,20,90,70),Color.Red,Color.Blue,0f)) g.FillRectangle(gradient,20,20,90,70);
            using (var path = new GraphicsPath()) {
                path.AddEllipse(140,20,70,70);
                using (var gradient = new PathGradientBrush(path)) {
                    gradient.CenterColor=Color.Green; gradient.SurroundColors=new Color[]{Color.Yellow}; g.FillPath(gradient,path);
                }
            }
            using (var hatch = new HatchBrush(HatchStyle.Cross,Color.Purple,Color.White)) g.FillRectangle(hatch,20,130,90,60);
            using (var image = new Bitmap(4,4)) using (var texture = new TextureBrush(image)) g.FillRectangle(texture,140,130,60,60);
        } else if (scene == "mixed") {
            g.FillRectangle(Brushes.Red,20,20,40,40);
            var dc=g.GetHdc(); try { MoveToEx(dc,100,20,IntPtr.Zero); LineTo(dc,150,70); } finally { g.ReleaseHdc(dc); }
            g.FillRectangle(Brushes.Blue,180,20,40,40);
        }
    }
    // 分别保存 Only、Dual 和由原生 GDI+ 回放得到的参考 PNG。
    public static void Generate(string directory) {
        foreach(var scene in new[]{"geometry","state","images","text","fallback","mixed"}) {
            foreach(var type in new[]{EmfType.EmfPlusOnly,EmfType.EmfPlusDual}) {
                string stem=Path.Combine(directory,scene+(type==EmfType.EmfPlusOnly?"-only":"-dual"));
                using(var surface=new Bitmap(256,256)) using(var reference=Graphics.FromImage(surface)) {
                    var dc=reference.GetHdc();
                    try {
                        using(var metafile=new Metafile(stem+".emf",dc,new RectangleF(0,0,256,256),MetafileFrameUnit.Pixel,type))
                        using(var graphics=Graphics.FromImage(metafile)) Draw(graphics,scene);
                    } finally { reference.ReleaseHdc(dc); }
                }
                using(var metafile=new Metafile(stem+".emf")) using(var image=new Bitmap(256,256)) {
                    image.SetResolution(96,96);
                    using(var graphics=Graphics.FromImage(image)) graphics.DrawImage(metafile,new Rectangle(0,0,256,256));
                    image.Save(stem+".png",ImageFormat.Png);
                }
            }
        }
    }
}
'@
[EmfPlusFixtures]::Generate((Resolve-Path $OutputDirectory).Path)
